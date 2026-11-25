# NeuroMotion

NeuroMotion 包含 Python 桌面客户端、STM32 MCU 固件和系统说明文档。客户端支持原双通道和双 ADS1292R 四通道实时采集，并整合视频导入、五反光点追踪、像素/毫米坐标换算、LED→TTL、时间对齐与同步回放。客户端自动识别两种采集协议。

四通道版本保留两块 ADC 的独立时钟，用同一 MCU timer 捕获每次 DRDY，每秒更新时钟模型，自动生成约延后 0.5 秒的统一 1000 Hz 四通道 CSV。原生 ADC 码和串口二进制单独保存，缺失通道保留 NaN 和质量标记。四通道主表是软件对齐和重采样结果；真实模拟同步精度须经同源台架测试确认，不能用网格间隔代替精度验收。

| 目录 | 内容 |
| --- | --- |
| [NeuroMotion_GUI](NeuroMotion_GUI/README.md) | GUI、命令行入口、Python 依赖、配置、测试、辅助脚本，以及 `calibration/`、`sessions/`、`output/` 数据。 |
| [NeuroMotion_MCU](NeuroMotion_MCU/README.md) | `ads1292_f446re/` 原 MCU 工程及 `.ioc` 引脚配置，以及 `mcu_diagnosis/` 隔离诊断工程。 |
| [Docs](Docs/README.md) | 输入协议与实验步骤；本地另保留不提交的计划、验证报告和诊断记录。 |

Windows 日常启动：直接双击 **`NeuroMotion_GUI/NeuroMotion.exe`**。EXE 包含运行依赖，无需 CMD 或本地 Python；源码仓库不提交构建后的二进制文件；预编译下载见 [GitHub Releases](https://github.com/XLVIII-06/NeuroMotion/releases)。

从源码运行或构建 EXE 时，先在系统根目录运行：

```powershell
Set-Location .\NeuroMotion_GUI
python -m pip install -r requirements.txt
python main.py gui
```

使用 `python scripts/build_windows.py` 构建前先安装 `NeuroMotion_GUI/requirements-build.txt`。也可以从 `NeuroMotion_GUI` 文件夹启动其中的 CMD 或 PYW。详细步骤见 [GUI 使用说明](NeuroMotion_GUI/README.md)。

MCU 主工程为 [ads1292_f446re.uvprojx](NeuroMotion_MCU/ads1292_f446re/MDK-ARM/ads1292_f446re.uvprojx)，包含 `ads1292_f446re` 双通道、`NeuroMotion_4CH` 原 PA0/PA1 四通道和 `NeuroMotion_4CH_Right` 右侧 PB8/PB2 四通道三个 target。右侧方案将共享 START 移到 PB4，仍用同一 TIM2 硬件时间轴。原 [CubeMX 配置](NeuroMotion_MCU/ads1292_f446re/ads1292_f446re.ioc) 对应双通道，四通道外设初始化独立维护。升级前按 [四通道接线与使用](Docs/FOUR_CHANNEL_GUIDE.md) 核对接线与目标。

Git 跟踪源码、依赖、构建脚本、自动化测试和使用说明；计划与测试报告、实验数据、个人配置、标定结果、生成的诊断工程和构建产物只保留本地。ST/ARM 驱动的原始许可证随驱动保留。MCU 开发使用本目录下的主工程。
