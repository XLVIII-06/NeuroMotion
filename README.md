# NeuroMotion

NeuroMotion 是完整的数据采集系统，包含 Python 桌面客户端、STM32 MCU 固件和系统说明文档。客户端负责实时双通道采集、视频分析、标定、时间对齐与同步回放，MCU 负责 ADS1292R 采样及同步信号输出。

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

使用 `python scripts/build_windows.py` 构建前先安装 `NeuroMotion_GUI/requirements-build.txt`；详细步骤见 [GUI 使用说明](NeuroMotion_GUI/README.md)。原有 `Start_NeuroMotion.cmd` 和 `NeuroMotion.pyw` 保留兼容，CMD 优先启动已有 EXE。其他命令行和测试命令均在 `NeuroMotion_GUI` 中运行。

MCU 主工程为 [ads1292_f446re.uvprojx](NeuroMotion_MCU/ads1292_f446re/MDK-ARM/ads1292_f446re.uvprojx)，引脚及外设配置为 [ads1292_f446re.ioc](NeuroMotion_MCU/ads1292_f446re/ads1292_f446re.ioc)。PWDN 应可靠连接 PC8；正常固件已通过真实 GUI 5 分钟采集及整套断电重启复核。当前使用备用 ADS 模块和独立 3.3 V，配对五点视频全链路实验仍待实测，具体引脚见 [MCU 说明](NeuroMotion_MCU/README.md)。

Git 跟踪源码、依赖、构建脚本、自动化测试和使用说明；计划与测试报告、实验数据、个人配置、标定结果、生成的诊断工程和构建产物只保留本地。ST/ARM 驱动的原始许可证随驱动保留。MCU 开发使用本目录下的主工程。
