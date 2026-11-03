# SDK

1. Configure video stream credentials:
```sdk/ameba-rtos-pro2/component/example/kvs_producer_mmf/sample_config.h```
```
sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/GCC-RELEASE/config.cmake
# Change 
set(mbedtls "mbedtls-2.28.1")
to:
set(mbedtls "mbedtls-2.16.6")
```


2. Configure video stream producer sdk:
```sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/src/amazon_kvs/lib_amazon```

```
# Command(s)

cd sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/src/amazon_kvs/lib_amazon
git clone -b v1.0.1 --recursive https://github.com/aws-samples/amazon-kinesis-video-streams-producer-embedded-c.git producer
```


3. Configure sensor:
```sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/inc/sensor.h```
4. 