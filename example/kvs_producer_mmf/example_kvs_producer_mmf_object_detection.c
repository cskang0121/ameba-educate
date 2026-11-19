/******************************************************************************
 * KVS producer + object detection  (milestone M4)
 *
 * WHAT IT DOES
 *   One camera, two pipelines:
 *     Pipeline 1  streams 1080p H264 video to AWS Kinesis Video Streams.
 *     Pipeline 2  runs YOLO on a small copy of the picture.
 *   For each YOLO result, nn_set_object() draws boxes on the streamed video
 *   (pipeline 3, overlay) for the WANTED classes only, and prints TRIGGER at
 *   most once per COOLDOWN_MS. M5 will take a JPEG snapshot at the trigger.
 *
 * DATA FLOW
 *   camera -+- ch0 1080p H264 -[queue 10]-> link task ------> KVS task --> AWS   (P1)
 *           |     ^ boxes drawn on top (P3: canvas -> OSD task)
 *           +- ch4 416x416 RGB -[queue 2]-> link task p3 -> YOLO                 (P2)
 *                                                            |
 *                                                    nn_set_object()
 *                                                     |- draw boxes (P3)
 *                                                     '- TRIGGER (cooldown)
 *   console: ATKVS=person,dog -> fATKVS() -> wanted_list -> read by nn_set_object()
 *
 * TASKS (who runs what)
 *   setup task      prio 1   builds everything below, then deletes itself
 *   P1 link task    SDK      moves ch0 frames to the KVS module
 *   KVS task        ~1       uploads to AWS (created by CMD_KVS_PRODUCER_SET_APPLY)
 *   P2 link task    3        feeds YOLO; nn_set_object() runs INSIDE this task
 *   OSD task        SDK      paints the published boxes onto ch0
 *   console task    SDK      runs fATKVS() when you type ATKVS
 *   idle            0
 *
 * OUTSIDE THIS FILE
 *   sample_config.h        : #define ENABLE_AUDIO_TRACK 0  (else AWS error 4011)
 *   kvs_producer_mmf.cmake : osd_render.c line uncommented
 *   build / flash          : cmake --build . --target flash_nn  ->  flash_ntz.nn.bin
 *
 * AT COMMANDS (serial terminal)
 *   ATKVS                  show the wanted classes
 *   ATKVS=person,dog       set them (comma-separated COCO names, e.g. "cell phone")
 *
 * KNOWN ISSUES
 *   - "[OD] VIPNN opened" prints even if the model failed to load (return not checked).
 *   - wanted_list is shared by two tasks without a lock (see "Wanted list").
 *   - Error paths (goto out) do not close modules that were already opened.
 ******************************************************************************/

/* =========================================================================
 * Includes
 * ========================================================================= */

// --- Base: printf, FreeRTOS tasks, our entry prototype ---
#include <stdio.h>
#include "FreeRTOS.h"
#include "task.h"
#include "example_kvs_producer_mmf.h"

// --- P1: Wi-Fi wait, camera module, link (SISO), KVS module ---
#include "wifi_conf.h"
#include "lwip_netconf.h"
#include "module_video.h"
#include "mmf2_pro2_video_config.h"
#include "mmf2_siso.h"
#include "mmf2_link.h"
#include "module_kvs_producer.h"

// --- P2: neural-network module and the YOLO model description ---
#include "avcodec.h"
#include "module_vipnn.h"
#include "model_yolo.h"

// --- P3: overlay drawing (canvas_*, osd_render_*) and class names ---
#include "osd_render.h"
#include "nn_utils/class_name.h"

// --- Wanted list: AT command registration, strcmp ---
#include "log_service.h"
#include <string.h>

/* =========================================================================
 * Pipeline 1: channel 0 -> KVS  (settings + handles)
 * ========================================================================= */

// --- P1 settings: 1080p, 15 fps, one key frame per second, 1 Mbit/s VBR ---
#define V1_CHANNEL   0
#define V1_WIDTH     1920
#define V1_HEIGHT    1080
#define V1_FPS       15
#define V1_GOP       15
#define V1_BPS       (1024 * 1024)
#define V1_RCMODE    2

static video_params_t video_v1_params = {
	.stream_id       = V1_CHANNEL,
	.type            = VIDEO_H264,
	.resolution      = VIDEO_FHD,
	.width           = V1_WIDTH,
	.height          = V1_HEIGHT,
	.bps             = V1_BPS,
	.fps             = V1_FPS,
	.gop             = V1_GOP,
	.rc_mode         = V1_RCMODE,
	.use_static_addr = 1
};

// --- P1 handles: file level so they outlive the setup task ---
static mm_context_t *video_v1_ctx        = NULL;
static mm_context_t *kvs_producer_ctx    = NULL;
static mm_siso_t    *siso_video_kvs      = NULL;

/* =========================================================================
 * Pipeline 2: channel 4 -> YOLO  (settings + handles)
 * ========================================================================= */

// --- P2 camera settings: whole 1920x1080 sensor shrunk to 416x416 raw RGB ---
#define NN_CHANNEL         4
#define NN_WIDTH           416
#define NN_HEIGHT          416
#define NN_FPS             15
#define NN_BPS             (1024 * 1024)
#define SENSOR_MAX_WIDTH   1920
#define SENSOR_MAX_HEIGHT  1080

static video_params_t video_v4_params = {
	.stream_id       = NN_CHANNEL,
	.type            = VIDEO_RGB,
	.resolution      = VIDEO_VGA,
	.width           = NN_WIDTH,
	.height          = NN_HEIGHT,
	.bps             = NN_BPS,
	.fps             = NN_FPS,
	.gop             = NN_FPS,
	.direct_output   = 0,
	.use_static_addr = 1,
	.use_roi         = 1,
	.roi = {
		.xmin = 0,
		.ymin = 0,
		.xmax = SENSOR_MAX_WIDTH,
		.ymax = SENSOR_MAX_HEIGHT,
	}
};

// --- P2 model input: must match video_v4_params (size and RGB format) ---
static nn_data_param_t roi_nn = {
	.img = {
		.width  = NN_WIDTH,
		.height = NN_HEIGHT,
		.rgb    = 0,
		.roi = {
			.xmin = 0,
			.ymin = 0,
			.xmax = NN_WIDTH,
			.ymax = NN_HEIGHT,
		}
	},
	.codec_type = AV_CODEC_ID_RGB888
};

// --- P2 filters: passed by address because ctrl() only takes an int ---
static float nn_confidence_thresh = 0.5;	// drop detections below 50 %
static float nn_nms_thresh        = 0.3;	// merge overlapping boxes

#define PERSON_CLASS_ID  0				// COCO class 0 = person

// --- P2 handles ---
static mm_context_t *video_rgb_ctx       = NULL;
static mm_context_t *vipnn_ctx           = NULL;
static mm_siso_t    *siso_video_vipnn    = NULL;

/* =========================================================================
 * Pipeline 3: overlay on channel 0  (look of the boxes)
 * ========================================================================= */

// --- P3 where to draw: channel 0 (the video that goes to KVS), layer 0 ---
#define OSD_CHANNEL   V1_CHANNEL
#define OSD_BLOCK     0

// --- P3 look: change only these four lines ---
#define BOX_LINE      6
#define BOX_COLOR     COLOR_RED
#define LABEL_COLOR   COLOR_RED
#define OSD_FORMAT    RTS_OSD2_BLK_FMT_RGBA2222

/* =========================================================================
 * Wanted list + trigger  (M4)
 *
 * Written by fATKVS() in the console task, read by nn_set_object() in the
 * P2 link task. Entries are written before the count as a light guard; it is
 * NOT a real lock. Proper fix: taskENTER_CRITICAL()/taskEXIT_CRITICAL()
 * around the write in fATKVS() and around a local copy in nn_set_object().
 * ========================================================================= */

// --- Trigger settings: one trigger per 5 s at most (tick = 1 ms here) ---
#define COOLDOWN_MS    5000
#define MAX_CLASSES    80

// --- Shared state ---
static int wanted_list[MAX_CLASSES] = {PERSON_CLASS_ID};	// starts as [person]
static volatile int wanted_count    = 1;
static uint32_t last_trigger_tick   = 0;				// 0 = never triggered

// --- is_wanted: "is this class number in wanted_list?" ---
static int is_wanted(int class_id)
{
	int n = wanted_count;
	for (int i = 0; i < n; i++) {
		if (wanted_list[i] == class_id) {
			return 1;
		}
	}
	return 0;
}

// --- fATKVS: runs in the console task when you type ATKVS[=names] ---
static void fATKVS(void *arg)
{
	char *argv[MAX_ARGC] = {0};
	int argc = 0;

	// Step 1: split "person,dog" into argv[1], argv[2] ... (argv[0] unused)
	if (arg) {
		argc = parse_param((char *)arg, argv);
	}

	// Step 2: no names given -> just print the current list
	if (argc <= 1) {
		printf("\r\n[OD] wanted classes:");
		for (int i = 0; i < wanted_count; i++) {
			printf(" %s", coco_name_get_by_id(wanted_list[i]));
		}
		printf("\r\n");
		return;
	}

	// Step 3: turn each name into its class number; report unknown names
	int new_list[MAX_CLASSES];
	int new_count = 0;
	for (int j = 1; j < argc; j++) {
		int found = -1;
		for (int c = 0; c < MAX_CLASSES; c++) {
			if (argv[j] && strcmp(argv[j], coco_name_get_by_id(c)) == 0) {
				found = c;
				break;
			}
		}
		if (found >= 0) {
			new_list[new_count++] = found;
		} else {
			printf("\r\n[OD] unknown class: %s\r\n", argv[j] ? argv[j] : "(empty)");
		}
	}

	// Step 4: nothing valid -> keep the old list
	if (new_count == 0) {
		printf("\r\n[OD] no valid class, list unchanged\r\n");
		return;
	}

	// Step 5: publish the new list (entries first, count last)
	for (int i = 0; i < new_count; i++) {
		wanted_list[i] = new_list[i];
	}
	wanted_count = new_count;

	printf("\r\n[OD] wanted classes:");
	for (int i = 0; i < wanted_count; i++) {
		printf(" %s", coco_name_get_by_id(wanted_list[i]));
	}
	printf("\r\n");
}

// --- Command table: static because the SDK keeps a pointer to it ---
static log_item_t kvs_items[] = {
	{"ATKVS", fATKVS, },
};

/* =========================================================================
 * nn_set_object: the YOLO callback
 *
 * Called by the NN module after EVERY picture (~7.5 per second), inside the
 * P2 link task (prio 3). Keep it short: while it runs, YOLO cannot take the
 * next picture. Never block here (no network, no long waits).
 *   p          -> detections (vipnn_out_buf_t)
 *   img_param  -> the NN input description (roi_nn), used for scaling
 * ========================================================================= */
static void nn_set_object(void *p, void *img_param)
{
	vipnn_out_buf_t *out = (vipnn_out_buf_t *)p;
	objdetect_res_t *res = (objdetect_res_t *)&out->res[0];

	static int last_state = -1;		// remembers the last printed state between calls

	if (!p || !img_param) {
		return;
	}

	// --- Step 1: scale factors from the 416x416 model picture to the 1920x1080 video ---
	nn_data_param_t *im = (nn_data_param_t *)img_param;
	float ratio_h = (float)V1_HEIGHT / (float)im->img.height;
	float ratio_w = (float)V1_WIDTH  / (float)im->img.width;
	int roi_h = (int)((im->img.roi.ymax - im->img.roi.ymin) * ratio_h);
	int roi_w = (int)((im->img.roi.xmax - im->img.roi.xmin) * ratio_w);
	int roi_x = (int)(im->img.roi.xmin * ratio_w);
	int roi_y = (int)(im->img.roi.ymin * ratio_h);

	// --- Step 2: start a blank overlay (clears last picture's boxes) ---
	canvas_create_bitmap(OSD_CHANNEL, OSD_BLOCK, OSD_FORMAT);

	// --- Step 3: for each WANTED detection, draw box + label ---
	//     result[0] class id, [1] confidence 0..1, [2..5] box as fractions 0..1
	int person_found = 0;			// "a wanted object was found" (name kept from M2)
	int best_score = 0;
	int best_class = -1;
	for (int i = 0; i < out->res_cnt; i++) {
		int class_id = (int)res[i].result[0];
		if (is_wanted(class_id)) {
			int score = (int)(res[i].result[1] * 100);
			if (score > best_score) {
				best_score = score;
				best_class = class_id;
			}
			person_found = 1;

			int xmin = (int)(res[i].result[2] * roi_w) + roi_x;
			int ymin = (int)(res[i].result[3] * roi_h) + roi_y;
			int xmax = (int)(res[i].result[4] * roi_w) + roi_x;
			int ymax = (int)(res[i].result[5] * roi_h) + roi_y;
			if (xmin < 0) xmin = 0;
			if (ymin < 0) ymin = 0;
			if (xmax > V1_WIDTH)  xmax = V1_WIDTH;
			if (ymax > V1_HEIGHT) ymax = V1_HEIGHT;

			canvas_set_rect(OSD_CHANNEL, OSD_BLOCK, xmin, ymin, xmax, ymax, BOX_LINE, BOX_COLOR);

			char label[20];
			snprintf(label, sizeof(label), "%s %d", coco_name_get_by_id(class_id), score);
			canvas_set_text(OSD_CHANNEL, OSD_BLOCK, xmin, ymin - 32, label, LABEL_COLOR);	// NOTE: may go off-screen at the top
		}
	}

	// --- Step 4: publish the drawing; the OSD task paints it onto ch0 ---
	canvas_update(OSD_CHANNEL, OSD_BLOCK, 1);

	// --- Step 5: TRIGGER if a wanted object is in view and the cooldown is over ---
	//     unsigned (now - last) stays correct when the tick counter wraps
	if (person_found) {
		uint32_t now = xTaskGetTickCount();
		if (last_trigger_tick == 0 || (now - last_trigger_tick) >= COOLDOWN_MS) {
			last_trigger_tick = now;
			printf("\r\n[OD] TRIGGER %s (%d%%)\r\n", coco_name_get_by_id(best_class), best_score);
			// M5: request the snapshot here
		}
	}

	// --- Step 6: log only when the answer changes (avoids 7 lines per second) ---
	if (person_found != last_state) {
		last_state = person_found;
		if (person_found) {
			printf("\r\n[OD] wanted object detected (%d%%)\r\n", best_score);
		} else {
			printf("\r\n[OD] none of the wanted objects\r\n");
		}
	}
}

/* =========================================================================
 * wifi_wait_connected: block until joined AND an IP address is assigned
 * Uses vTaskDelay, so it must run in a task (after the scheduler starts).
 * ========================================================================= */
static void wifi_wait_connected(void)
{
	uint32_t waited = 0;

	while (!((wifi_get_join_status() == RTW_JOINSTATUS_SUCCESS) &&
			 (*(u32 *)LwIP_GetIP(0) != IP_ADDR_INVALID))) {
		vTaskDelay(10);
		waited++;
		if (waited == 500) {
			printf("\r\n[OD] no Wi-Fi yet. Use ATW0=<ssid>, ATW1=<password>, ATWC\r\n");
		}
	}
	printf("\r\n[OD] Wi-Fi ready\r\n");
}

/* =========================================================================
 * Setup task: builds all pipelines, then deletes itself
 *
 * Rule for every pipeline: open the sink first, then the link, and start the
 * camera channel LAST, so no frame arrives with nowhere to go.
 * On any failure: goto out (the task must never return).
 * ========================================================================= */
static void example_kvs_producer_mmf_object_detection_thread(void *param)
{
	printf("\r\n[OD] entry reached\r\n");

	// --- Setup 0.1: register ATKVS first, so it works while Wi-Fi connects ---
	log_service_add_table(kvs_items, sizeof(kvs_items) / sizeof(kvs_items[0]));

	// --- Setup 0.2: wait for the network (KVS needs it) ---
	wifi_wait_connected();

	// --- Setup 0.3: plan video memory for EVERY channel used (ch0 and ch4) ---
	int voe_heap_size = video_voe_presetting(1, V1_WIDTH, V1_HEIGHT, V1_BPS, 0,
											 0, 0, 0, 0, 0,
											 0, 0, 0, 0, 0,
											 1, NN_WIDTH, NN_HEIGHT);
	printf("\r\n[OD] voe heap size = %d\r\n", voe_heap_size);

	// --- Pipeline 1.1: open camera ch0, store settings, build its 10-slot queue ---
	video_v1_ctx = mm_module_open(&video_module);
	if (video_v1_ctx) {
		mm_module_ctrl(video_v1_ctx, CMD_VIDEO_SET_PARAMS, (int)&video_v1_params);
		mm_module_ctrl(video_v1_ctx, MM_CMD_SET_QUEUE_LEN, 10);
		mm_module_ctrl(video_v1_ctx, MM_CMD_INIT_QUEUE_ITEMS, MMQI_FLAG_DYNAMIC);
	} else {
		printf("\r\n[OD] video open fail\r\n");
		goto out;
	}

	// --- Pipeline 1.2: open KVS; SET_APPLY creates the KVS upload task ---
	kvs_producer_ctx = mm_module_open(&kvs_producer_module);
	if (kvs_producer_ctx) {
		mm_module_ctrl(kvs_producer_ctx, CMD_KVS_PRODUCER_SET_APPLY, 0);
	} else {
		printf("\r\n[OD] KVS open fail\r\n");
		goto out;
	}

	// --- Pipeline 1.3: link ch0 -> KVS; siso_start creates the P1 link task ---
	siso_video_kvs = siso_create();
	if (siso_video_kvs) {
		siso_ctrl(siso_video_kvs, MMIC_CMD_ADD_INPUT,  (uint32_t)video_v1_ctx, 0);
		siso_ctrl(siso_video_kvs, MMIC_CMD_ADD_OUTPUT, (uint32_t)kvs_producer_ctx, 0);
		siso_start(siso_video_kvs);
	} else {
		printf("\r\n[OD] siso open fail\r\n");
		goto out;
	}

	// --- Pipeline 1.4: start ch0 last; frames start flowing to KVS ---
	mm_module_ctrl(video_v1_ctx, CMD_VIDEO_APPLY, V1_CHANNEL);
	printf("\r\n[OD] M1 pipeline running: ch0 -> KVS\r\n");

	// --- Pipeline 2.1: open camera ch4 (same module, new instance), 2-slot queue ---
	//     short queue on purpose: YOLO is slower than the camera, so this caps
	//     how old a waiting picture can get (about 2 x YOLO time)
	video_rgb_ctx = mm_module_open(&video_module);
	if (video_rgb_ctx) {
		mm_module_ctrl(video_rgb_ctx, CMD_VIDEO_SET_PARAMS, (int)&video_v4_params);
		mm_module_ctrl(video_rgb_ctx, MM_CMD_SET_QUEUE_LEN, 2);
		mm_module_ctrl(video_rgb_ctx, MM_CMD_INIT_QUEUE_ITEMS, MMQI_FLAG_DYNAMIC);
	} else {
		printf("\r\n[OD] video rgb open fail\r\n");
		goto out;
	}

	// --- Pipeline 2.2: open the NN: model, input, callback, filters, then load ---
	vipnn_ctx = mm_module_open(&vipnn_module);
	if (vipnn_ctx) {
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_SET_MODEL, (int)&yolov4_tiny);
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_SET_IN_PARAMS, (int)&roi_nn);
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_SET_DISPPOST, (int)nn_set_object);	// register the callback
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_SET_CONFIDENCE_THRES, (int)&nn_confidence_thresh);
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_SET_NMS_THRES, (int)&nn_nms_thresh);
		mm_module_ctrl(vipnn_ctx, CMD_VIPNN_APPLY, 0);		// loads the model from flash
	} else {
		printf("\r\n[OD] VIPNN open fail\r\n");
		goto out;
	}
	printf("\r\n[OD] VIPNN opened\r\n");		// NOTE: printed even if APPLY failed

	// --- Pipeline 2.3: link ch4 -> NN; 64 KB stack, prio 3 (set BEFORE siso_start) ---
	siso_video_vipnn = siso_create();
	if (siso_video_vipnn) {
		siso_ctrl(siso_video_vipnn, MMIC_CMD_ADD_INPUT,  (uint32_t)video_rgb_ctx, 0);
		siso_ctrl(siso_video_vipnn, MMIC_CMD_SET_STACKSIZE, (uint32_t)1024 * 64, 0);
		siso_ctrl(siso_video_vipnn, MMIC_CMD_SET_TASKPRIORITY, 3, 0);
		siso_ctrl(siso_video_vipnn, MMIC_CMD_ADD_OUTPUT, (uint32_t)vipnn_ctx, 0);
		siso_start(siso_video_vipnn);
	} else {
		printf("\r\n[OD] siso vipnn open fail\r\n");
		goto out;
	}

	// --- Pipeline 2.4: start ch4 last, then switch on its raw RGB output ---
	mm_module_ctrl(video_rgb_ctx, CMD_VIDEO_APPLY, NN_CHANNEL);
	mm_module_ctrl(video_rgb_ctx, CMD_VIDEO_YUV, 2);		// value 2 copied from the SDK example
	printf("\r\n[OD] M2 pipeline running: ch4 -> YOLO\r\n");

	// --- Pipeline 3.1: start the overlay on ch0 (arrays = channels 0, 1, 2) ---
	//     NOTE: safer to do this before Pipeline 2.4, so the callback never
	//     draws before the overlay exists
	int ch_enable[3]     = {1, 0, 0};
	int char_resize_w[3] = {16, 0, 0};
	int char_resize_h[3] = {32, 0, 0};
	int ch_width[3]      = {V1_WIDTH, 0, 0};
	int ch_height[3]     = {V1_HEIGHT, 0, 0};
	osd_render_dev_init(ch_enable, char_resize_w, char_resize_h);
	osd_render_task_start(ch_enable, ch_width, ch_height);
	printf("\r\n[OD] M3 overlay running: boxes on ch0\r\n");

	// --- Done: delete the setup task; the pipelines keep running in their own tasks ---
out:
	vTaskDelete(NULL);
}

/* =========================================================================
 * Entry point: called from app_example() BEFORE the scheduler starts.
 * Only creates the setup task (which can block); never do the setup here.
 * ========================================================================= */
void example_kvs_producer_mmf_object_detection(void)
{
	if (xTaskCreate(example_kvs_producer_mmf_object_detection_thread,
					((const char *)"example_kvs_producer_mmf_object_detection_thread"),
					4096, NULL, tskIDLE_PRIORITY + 1, NULL) != pdPASS) {
		printf("\r\n[OD] create thread failed\r\n");
	}
}