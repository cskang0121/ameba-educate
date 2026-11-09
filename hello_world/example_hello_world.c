/* FreeRTOS includes. */
#include "FreeRTOS.h"
#include "task.h"
#include "platform_stdlib.h"

#include "example_hello_world.h"

/*
 * Minimal example to confirm that building and flashing work.
 * Waits 10 seconds (so start-up logs finish and the serial console can be opened),
 * then prints "Hello World!" three times, once per second, and ends the task.
 */
#define HELLO_WORLD_START_DELAY_MS  10000

static void example_hello_world_thread(void *param)
{
	(void)param;

	vTaskDelay(pdMS_TO_TICKS(HELLO_WORLD_START_DELAY_MS));

	for (unsigned int count = 1; count <= 3; count++) {
		printf("\r\nHello World! count=%u\r\n", count);
		vTaskDelay(pdMS_TO_TICKS(1000));
	}

	vTaskDelete(NULL);
}

void example_hello_world(void)
{
	if (xTaskCreate(example_hello_world_thread, "example_hello_world_thread", 512, NULL, tskIDLE_PRIORITY + 1, NULL) != pdPASS) {
		printf("\n\r%s xTaskCreate example_hello_world_thread failed", __FUNCTION__);
	}
}
