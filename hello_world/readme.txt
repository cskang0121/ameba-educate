HELLO WORLD EXAMPLE

Description
~~~~~~~~~~~
    Minimal example to confirm that building and flashing work.
    No wiring or Wi-Fi needed. A FreeRTOS task waits 10 seconds, prints three lines, one per second, then exits.

Setup
~~~~~
    This folder lives at the repo root. bootstrap.sh copies it to
    sdk/ameba-rtos-pro2/component/example/hello_world after cloning the SDK.
    If the SDK copy already exists, bootstrap.sh skips it and never overwrites it.
    To pick up edits made here, delete the SDK's copy and re-run ./bootstrap.sh, or copy it by hand:
    cp -r hello_world sdk/ameba-rtos-pro2/component/example/

Build (In WSL, from the repo root)
~~~~~
    cd sdk/ameba-rtos-pro2/project/realtek_amebapro2_v0_example/GCC-RELEASE
    
    rm -rf build && mkdir build && cd build \
    && cmake .. -G"Unix Makefiles" -DCMAKE_TOOLCHAIN_FILE=../toolchain.cmake -DEXAMPLE=hello_world \
    && cmake --build . --target flash -j$(nproc)

Flash (In PowerShell, from the repo root)
~~~~~
    Put the board in download mode, then (replace COM5 with your board's port):
    cd sdk/ameba-rtos-pro2/tools/image_tool/Pro2_PG_tool_v1.4.3
   
    .\uartfwburn.exe -p COM5 -f ..\..\..\project\realtek_amebapro2_v0_example\GCC-RELEASE\build\flash_ntz.bin -b 2000000 -U -x 32 -r

    Reset the board after flashing, then open the serial console.

Result
~~~~~~
    Serial console at 115200. After "== RAM Start ==" and the usual start-up logs:

    Hello World! count=1
    Hello World! count=2
    Hello World! count=3
