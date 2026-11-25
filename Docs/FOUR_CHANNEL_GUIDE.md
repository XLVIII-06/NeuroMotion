# 双 ADS1292R 四通道

本版本让 NUCLEO-F446RE 连续接收两块 ADS1292R 的独立 1000 samples/s 数据。两板共享一次 START，保留板载独立时钟。MCU 给每次 DRDY 打同一 timer 时间戳，Python 每秒更新时钟模型并重采样为公共 1000 Hz 网格。约 0.5 秒缓存用于等待另一板及 FIR 支持区间，原生二进制和 ADC 码即时保存。

## 断电接线

使用两侧均为 1–30 行的面包板时，参考 [NeuroMotion 系统接线图](NEUROMOTION_SYSTEM_WIRING.html)。图按 V1.1 正面针序绘制，左排 c 列、右排 g 列，上方 A 占 1–9 行，下方 B 占 15–23 行。模块 GND/3V3 分别为 A 的 c8/c9、B 的 c22/c23。图中列出 MCU 排针、共享信号逐线接法和 LED 板连接。

先断开 NUCLEO USB 并关闭两板外部电源。确认两路电源均断开后改线。下表对应右侧引脚固件 **`NeuroMotion_4CH_Right`**，不能与原 `NeuroMotion_4CH` 接线混用；端口名以实物丝印为准。

| 模块信号 | 模块 A 的 MCU 引脚 | 模块 B 的 MCU 引脚 |
| --- | --- | --- |
| SCLK（串行时钟） | PA5 / D13 | PB3 / D3 |
| DIN | PA7 / D11 | PA7 / D11 |
| DOUT | PA6 / D12 | PA6 / D12 |
| CS | PB9 | PB6 |
| START | PB4 / D5 / CN10-27 | PB4 / D5 / CN10-27 |
| PWDN | PC8 | PC9 |
| DRDY | PB8 / D15 / CN10-3 | PB2 / CN10-22 |
| 3V3 | 外部稳压 3.3 V | 同一外部稳压 3.3 V |
| GND | 电源及 NUCLEO 共地 | 同一 GND |

原始 PA8 / D7 TTL 回接 PB10 / D6，LED 按系统接线图串限流电阻。数字捕获必须取 PA8 原始数字节点，不能取 0.8 V 分压、LED 阴极或模拟测试节点。两块板 CLK/CLKSEL 保持原模块使用状态，不将两块振荡器输出相连。NUCLEO 3V3 到 ADS 的正线保持断开。两路 PWDN 不能悬空。

右侧版本的 META 固件名为 `NeuroMotion-4ch-v2-right-drdy`，并记录 DRDY/START 引脚及捕获通道。A 的串行 SCLK 接 PA5，B 接 PB3，两路不能再短接在同一节点。MCU 仍使用 SPI1，但只将当前设备的 SCLK 引脚配置为 AF5，另一引脚保持低电平；DIN/DOUT 继续共享，CS 独立。PB3 在此版本用于 SPI，不能同时作 SWO；PB4 用作 START，不能作 JTAG NJTRST，SWD 下载引脚 PA13/PA14 不变。这与两模块的独立采样时钟 CLK 完全不同，CLK/CLKSEL 不改。

从原四通道迁移只移动三根 MCU 端线：A 的 j1 从 PA0 移到 PB8，B 的 j15 从 PA1 移到 PB2，共享 START 从 PB8 移到 PB4。先移走 PB8 上原 START 线，不能让 START 输出与 DRDY 返回线相连。保留两块 ADS 对 START 的共用连接。完成断电核线并准备合格模块后，才下载对应右侧固件并进行上电验证。

两个 DRDY 仍使用同一 32 位 TIM2、名义 1 MHz 计数器和原有 64 位溢出扩展：A=CH1，B=CH4，TTL=CH3。引脚复用依据 [ST DS10693 表11](https://www.st.com/resource/en/datasheet/stm32f446re.pdf)，右侧 CN10 位置依据 [UM1724 表29](https://www.st.com/resource/en/user_manual/dm00105823.pdf)。右侧引脚版启动时跳过旧双通道的 PC7 外部中断。

TTL 捕获采用 TIM2_CH3 输入滤波 `fDTS/32、连续 8 点`，对应名义 84 MHz timer 输入时钟下约 3 µs 的滤波时间；DRDY 捕获不采用这个滤波设置。META 的 `ttl_capture_filter` 记录其配置，STATUS 分别报告 `edge_overcapture`（捕获寄存器覆盖）、`edge_queue_dropped`（边沿队列满）及兼容的汇总 `edge_dropped`。这些计数不是可相加的真实丢边沿数量；出现异常须检查原始台账，不生成周期边沿掩盖损失。

右侧固件是 Keil `NeuroMotion_4CH_Right` target，HEX 位于 `NeuroMotion_MCU/ads1292_f446re/MDK-ARM/build/four_channel_right/NeuroMotion_4CH_Right.hex`。先核对并比对下载内容，恢复两路供电、等待约 3 秒稳定后按 RESET。启动约 5 秒后，仅在两板 ID/寄存器检查均成功时开始连续转换和起始编码。诊断状态中的 `ready_mask=3` 表示两板初始化通过，错误代码见 `dual_adc.c`。缺板时诊断帧仍持续发送，但不启动转换；修复接线后重新复位。

原 `NeuroMotion_4CH` target 继续保留原 PA0/PA1 DRDY、PB8 START、TIM2 CH1/CH2/CH3，固件名仍为 `NeuroMotion-4ch-v2-split-sclk`。其接线见 [PA0/PA1 参考图](SYSTEM_MODULE_BREADBOARD_PA0_PA1.html)。返回该 target 时须断电恢复这三根线；客户端串口协议不因引脚方案改变。

返回原双通道版本时，断电拆下 B，A 的 DRDY 恢复 PC7、共享 START 恢复 PB8、PWDN 保留 PC8，然后下载 `ads1292_f446re` target；客户端自动识别旧协议，不需另装旧客户端。

## 记录与查看

串口仍为 921600 baud、8N1。在客户端连接后先预览，确认四通道、A/B 原生计数和时钟状态正常。开始电脑记录和外部视频后，再手动 RESET，以完整记录起始 Barker 码。PC 的开始/停止只控制写盘，不发 ADC 起停命令。相机录像仍由外部设备完成。

实时页自动显示 CH1–CH4 和 TTL 五图。CH1/CH2 来自 A，CH3/CH4 来自 B。未滤波及滤波支路均明确标为已对齐；坐标设置、鼠标操作和最近 60 秒历史沿用原功能，纵轴默认独立，可四路联动。界面分别显示原生接收数、原生保存数和四通道网格行数，二者不是同一个计数。

每次记录重新拟合时钟：短预热后输出，约 20 秒后保存固定参考模型和偏移/频差作为对照，实际输出持续使用每秒更新的模型。偏移定义为同一原生序号处的 `T_B(n)-T_A(n)`，正值表示 B 更晚；报告记录所用 n，不能只看一个脱离参考点的毫秒值。模型不利用肌电波形相关性，不会主动把不同肌肉的激活时差消掉。

建议点击“EMG 20–200 Hz”应用相应显示/保存滤波支路。原生码及未滤波对齐值保留。滤波设置改变会记录生效行并重新预热，缺口后也重新预热；预热和缺失均有标记，不能当作测得的零值。MAD 默认关闭。

## 实验文件

| 文件（位于 acquisition/） | 用途 |
| --- | --- |
| serial.bin | 收到的完整字节流，可重放 |
| native_samples.csv | 每板原生序号、DRDY 时间、两路 ADC 码及状态 |
| emg.csv | 公共 1000 Hz 四通道未滤波/滤波值、TTL、每通道质量 |
| metadata.json | ADC 增益、参考电压、timer 频率和初始化读回 |
| sync_edges.csv | 同一 timer 实测的 TTL 边沿台账 |
| clock_models.jsonl | 持续时钟模型、残差和生效点 |
| clock_reference.json | 各 MCU 会话的固定参考及偏移/ppm |
| filter_events.jsonl | 滤波设置与生效网格行 |
| acquisition_report.json | 完整性、计数、时钟和硬件状态 |

异常中止会保留 `emg.partial.csv`。某一板缺样时，该板两通道填 NaN，另一板继续输出；FIR 必须有完整支持区间，因此缺口两侧的无效区域可能比原生缺口稍长。`valid`、`warming`、`native_gap`、`support_gap`、`spi_error`、`status_error`、`read_late`、`capture_error`、`source_stalled`、`invalid_native` 等标记说明原因。滤波质量另列。原生值不会被插值或 NaN 替换而丢弃其诊断记录。

离线对齐时选择本次 `emg.csv`，并将同目录 `sync_edges.csv` 保留一起。视频起始码必须完整，至少包含 10 个稳定边沿。公共零点由实测 TTL 首次上升沿定义，视频拟合使用硬件边沿，不使用串口到达时间。输出两种融合表包含四路及各自质量；回放显示四路 EMG、视频和五点位置。四通道 CSV 分块校验并使用磁盘索引，避免整份 30 分钟记录常驻内存。

## 台架验收范围

分别验证每块板后，按双板 30 秒、5 分钟、30 分钟逐级检查无异常丢样、捕获溢出及 UART/CRC 错误，并复核整套断电上电。时钟拟合残差小只能说明 DRDY 测时一致性；最终的跨板 ≤1 ms 目标仍需共同模拟输入测得。软件真值、数字时标和真实模拟精度分别记录。

共同测试源只能在无动物及 RLD 电极连接的台架上使用。当前 PGA=6、名义参考 2.42 V，差分理论满量程约 ±0.403 V。0–0.8 V 对地 TTL 不可直接接入 INP、INN 接地。需要经过已核对的衰减及中点偏置电路，目标约 5–20 mVpp 差分、共模约 1.65 V；未确认实物电平前不接模拟输入。

当前固件启用 A 的 RLD，关闭 B 的 RLD。模块原理图把标为 RLDOUT 的排针画成经 R8 接到芯片 RLDINV，实际驱动输出为芯片 30 脚。这两者在该图中属于不同节点。台架接线中两块板的 RLD 排针均留空。动物连接前须核实实物网络，并由实验室确认电极侧隔离和故障限流。若采用共用 RLD 偏置，只使用经过验证的单个驱动回路。

MCU 的名义微秒时钟尚未校准到 SI 秒，输出 1 ms 网格不能代替真实同步精度验收。
