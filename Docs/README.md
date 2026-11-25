# NeuroMotion 文档

使用说明见 [GUI 说明](../NeuroMotion_GUI/README.md) 与 [MCU 说明](../NeuroMotion_MCU/README.md)。本目录随仓库保留 [输入协议与时间轴](INPUT_FORMAT.md)、[四通道接线与使用](FOUR_CHANNEL_GUIDE.md)、[v2 字节协议](PROTOCOL_V2.md) 和 [实验检查清单](EXPERIMENT_CHECKLIST.md)。

实施计划、执行状态、台架测试说明与图纸、测试与诊断报告，以及购买模块附带的硬件手册保留在本地并由 `.gitignore` 排除。自动化测试源码在 `NeuroMotion_GUI/tests/`。实验数据和构建产物保留在本地。

文档中的 `output/`、`sessions/`、`scripts/` 和 Python 命令以 `NeuroMotion_GUI` 为工作目录。

当前四通道接线见 [NeuroMotion 系统接线图](NEUROMOTION_SYSTEM_WIRING.html)，包含 ADS 同排跳线、右侧 DRDY、TTL 回接和 LED/BL 分压。原 `NeuroMotion_4CH` 的接线见 [PA0/PA1 参考图](SYSTEM_MODULE_BREADBOARD_PA0_PA1.html)，两图配套固件不同。

BL-422I 第二通道 TTL、单脉冲标注、纸面参数表、三源融合和便携回放的使用步骤见 GUI 说明中的“BL-422I 刺激标注与三源回放”，字段和单位见输入协议。原始实验资料与标定输出继续仅保留本机。
