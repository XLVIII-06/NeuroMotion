# NeuroMotion MCU

主固件用于 NUCLEO-F446RE + ADS1292R，负责双通道采样、UART 发包和 LED/TTL 同步输出。打开 [Keil 工程](ads1292_f446re/MDK-ARM/ads1292_f446re.uvprojx) 构建及下载；[CubeMX 配置](ads1292_f446re/ads1292_f446re.ioc) 保存引脚、时钟和外设设置。原工程使用 ARMCLANG 6.24 与 Keil STM32F4xx_DFP 3.1.1；Core、Drivers 和 MDK-ARM 的相对位置需保持不变，ST/ARM 许可证随 Drivers 保留。

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

`mcu_diagnosis/overrides/` 与 GUI/scripts 中保留隔离诊断源码和工具，供定位 UART、DRDY、SPI 或寄存器异常使用；生成工程、诊断报告及含本机路径的 `active_run.json` 不进入 Git。历史诊断脚本依赖本机保存的基线和该清单，克隆仓库后不能直接沿用旧 RAM 地址或运行下载命令。日常采集使用主工程，无需生成诊断工程。
