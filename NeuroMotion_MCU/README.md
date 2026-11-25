# NeuroMotion MCU

主固件用于 NUCLEO-F446RE + ADS1292R，负责采样、UART 发包和 LED/TTL 同步输出。打开 [Keil 工程](ads1292_f446re/MDK-ARM/ads1292_f446re.uvprojx)，按接线选择 `ads1292_f446re`（单模块双通道）、`NeuroMotion_4CH`（原 PA0/PA1 四通道）或 `NeuroMotion_4CH_Right`（右侧 PB8/PB2 四通道）target。原工程使用 ARMCLANG 6.24 与 Keil STM32F4xx_DFP 3.1.1；Core、Drivers 和 MDK-ARM 的相对位置需保持不变，ST/ARM 许可证随 Drivers 保留。

[CubeMX 配置](ads1292_f446re/ads1292_f446re.ioc) 只描述原双通道配置。四通道通过 `NEUROMOTION_DUAL_ADC` 分支及 `Core/Src/dual_adc.c` 初始化共享 SPI、TIM2 捕获和 UART DMA，使用独立 4 KB 栈；不要用旧 `.ioc` 重新生成后直接覆盖四通道初始化。

可在本目录运行以下命令，仅构建，不会下载或复位设备。UV4 不在 PATH/安装记录中时，追加 `--uv4 <UV4.exe 的路径>`。

```powershell
python build_firmware.py --target all
```

原双通道 HEX 位于 `ads1292_f446re/MDK-ARM/ads1292_f446re/ads1292_f446re.hex`；原四通道位于 `ads1292_f446re/MDK-ARM/build/four_channel/NeuroMotion_4CH.hex`；右侧四通道位于 `ads1292_f446re/MDK-ARM/build/four_channel_right/NeuroMotion_4CH_Right.hex`。构建日志和 SHA256 清单位于 `MDK-ARM/build/`。可用 `--target legacy|four|right` 单独构建。更换 target 后应核对 HEX 路径与 Flash 比对结果。

## 原双通道接线

| ADS/同步引脚 | MCU 引脚 |
| --- | --- |
| SCLK | PA5 |
| DIN | PA7 / D11 |
| DOUT | PA6 / D12 |
| CS | PB9 |
| START | PB8 |
| PWDN | PC8 |
| DRDY | PC7 |
| TTL / LED | PA8 |

PWDN 两端必须可靠连接，不能悬空。已实际出现该线脱落导致芯片 ID 异常和采样停滞，接回 PC8 后恢复。当前验证条件为备用 ADS1292R 模块、独立 3.3 V 与共地，NUCLEO 的 3.3 V 正线与模块断开。正常固件通过了真实 GUI 5 分钟采集和整套断电上电后手动 RESET 的复核；真实 30 分钟和配对五点视频全链路仍待实测。

串口为 921600 baud、8N1，两路 nominal 1000 samples/s。复位初始化后等待约 5 秒，输出 Barker 起始编码并采样；电脑端的开始/停止记录不控制 MCU 起停。实验应先开始 GUI 记录和外部相机录像，再手动复位 MCU。数据协议见 [输入格式](../Docs/INPUT_FORMAT.md)。

## 新四通道接线与采样

两模块共享 SPI1 的 PA6 DOUT、PA7 DIN 和 START，各有片选与 PWDN。两个四通道 target 都要求 A SCLK→PA5(D13)、B SCLK→PB3(D3)，由 `route_sclk()` 在两个 AF5 输出引脚中只启用当前设备，另一个保持低；PB3 不作 SWO。右侧 target 的 DRDY 为 A→PB8（CN10-3/D15）、B→PB2（CN10-22），共享 START→PB4（CN10-27/D5）。原 target 保持 A→PA0、B→PA1、START→PB8。映射集中在 `Core/Inc/nm_capture_map.h`，`NEUROMOTION_RIGHT_DRDY` 选择右侧方案，`main.h` 同步选择 START GPIO。旧 `.ioc` 仍只描述双通道，不用于生成四通道配置。

右侧方案仍以 TIM2_CH1/CH4 捕获 DRDY、CH3 捕获 PB10 TTL，原方案为 CH1/CH2/CH3；计数器、溢出扩展、捕获边沿与 TTL 滤波不变。PA8 原始数字 TTL 回接 PB10，不能接 LED 分压后的节点。完整三根 MCU 端线迁移及对应图纸见 [四通道说明](../Docs/FOUR_CHANNEL_GUIDE.md)。两模块继续用各自板载采样时钟，CLK/CLKSEL 沿用原状态；分开的 SCLK 不是采样时钟。右侧目标启动时跳过旧双通道的 PC7 外部中断。

TIM2 以名义 1 MHz 硬件捕获两个 DRDY 及 TTL 边沿并扩展为 64 位。中断只入队，主循环按独立 CS 读取连续转换结果，USART2 使用 DMA1 Stream6 Channel4 发送 [v2 原生协议](../Docs/PROTOCOL_V2.md)。序号在捕获时推进，SPI/状态/过晚读取和捕获溢出均保留错误标记。只有两块 ADC 初始化及寄存器检查均通过才拉高共享 START；失败时仍发送诊断 META/STATUS。

TTL 捕获单独使用 fDTS/32、连续 8 点输入滤波，约 3 µs；DRDY 不变。边沿过捕获与队列满分别报告，META 保存滤波配置。分开 SCLK 与该 TTL 滤波的双板短测、五分钟及三十分钟真实记录、整套断电重启已通过；模拟输入时差另行验收。

模块 A 启用 RLD，模块 B 的 RLD 放大器和检测关闭。台架接线中两块板的 RLD 排针均留空。模块外部标记与芯片针脚的区别见[四通道说明](../Docs/FOUR_CHANNEL_GUIDE.md)。独立 3.3 V 与 MCU 共地，断开 NUCLEO 3V3 到模块的正线。PWDN A→PC8、B→PC9 均须可靠连接。共用 MCU 时标没有做 SI 秒绝对校准，原生采样率与板间 ppm 以该时标估计。

`mcu_diagnosis/overrides/` 与 GUI/scripts 中保留隔离诊断源码和工具，供定位 UART、DRDY、SPI 或寄存器异常使用；生成工程、诊断报告及含本机路径的 `active_run.json` 不进入 Git。历史诊断脚本依赖本机保存的基线和该清单，克隆仓库后不能直接沿用旧 RAM 地址或运行下载命令。日常采集使用主工程，无需生成诊断工程。
