# SDK

## Configure SDK mbedtls version
```
sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/GCC-RELEASE/config.cmake
```

```
set(mbedtls "mbedtls-2.16.6")                           # From default: set(mbedtls "mbedtls-2.28.1")
```

## Configure SDK sensor:
```sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/inc/sensor.h```

```
# Line ~224 to ~240
static const unsigned char sen_id[SENSOR_MAX] = {
	SENSOR_DUMMY,
	SENSOR_F37,                                         // From default: SENSOR_GC2053
	SENSOR_GC4653,
	SENSOR_GC4023,
	SENSOR_SC2333
};

#define USE_SENSOR      	SENSOR_F37                  // From default: SENSOR_GC2053

static const      char manual_iq[SENSOR_MAX][64] = {
	"iq",
	"iq_f37",                                           // From default: SENSOR_GC2053
	"iq_gc4653",
	"iq_gc4023",
	"iq_sc2333",
};
```


## Configure AWS Kinesis Video Stream credentials and setup:

```sdk/ameba-rtos-pro2/component/example/kvs_producer_mmf/sample_config.h```

```
/* Line ~6 to ~8*/
#define AWS_ACCESS_KEY                  "xxxxxxxxxxxxxxxxxxxx"
#define AWS_SECRET_KEY                  "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

/* Line ~17 */
#define ENABLE_AUDIO_TRACK              0

/* Line ~30 */
#define USE_AUDIO_AAC                   0

```

## Build firmware in WSL (1st time)
```
sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/GCC-RELEASE
```

```
mkdir build && cd build
cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DEXAMPLE=kvs_producer_mmf && cmake --build . --target flash -j$(nproc)
```

## Rebuild firmware in WSL (2nd time onwards)
```
cd .. && rm -rf build && mkdir build && cd build && cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DEXAMPLE=kvs_producer_mmf && cmake --build . --target flash -j$(nproc)
```

## Flash firmware in Powershell
```
sdk/ameba-rtos-pro2/tools/image_tool/Pro2_PG_tool_v1.4.3
```

```
.\uartfwburn.exe -p COM5 -f ..\..\..\project\realtek_amebapro2_v0_example\GCC-RELEASE\build\flash_ntz.bin -b 2000000 -U -x 32 -r
```
