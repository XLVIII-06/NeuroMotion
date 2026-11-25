# NeuroMotion GUI

NeuroMotion GUI 是 NeuroMotion 系统的 Python 桌面客户端，支持 NUCLEO-F446RE + 单 ADS1292R 双通道及双 ADS1292R 四通道，整合五反光点追踪、像素/毫米坐标换算、LED→TTL、公共时间轴对齐与同步回放。视频录像由外部设备完成，再导入客户端离线分析。电脑端控制连接和记录，MCU 仍由手动复位启动。

本说明中的命令均在 `NeuroMotion_GUI` 目录中运行；系统入口和目录概览见 [系统说明](../README.md)，MCU 工程见 [固件说明](../NeuroMotion_MCU/README.md)。

## 启动

Windows 用户可直接双击本目录的 **`NeuroMotion.exe`**。这是包含 Python 和运行依赖的独立程序，无需先运行 CMD 或安装 Python；首次启动需要解压运行库，等待数秒即可。默认记录保存在 EXE 同目录的 `sessions/`，处理结果在 `output/`，不要把程序放进无写入权限的目录。

源码运行方式：

```powershell
python -m pip install -r requirements.txt
python main.py gui
```

也可双击 `NeuroMotion.pyw` 或 `Start_NeuroMotion.cmd`。CMD 优先启动已有 EXE；没有 EXE 时优先使用本工程 `.venv`，否则使用系统 Python。启动错误写入 `startup_error.log`。界面使用 Python/Tkinter，实时数据保存独立于界面刷新。

在 Windows 上从源码构建 EXE：

```powershell
python -m pip install -r requirements-build.txt
python scripts/build_windows.py
```

构建临时文件和日志在 `build/windows/`，最终文件为本目录的 `NeuroMotion.exe`。脚本分别运行双通道和四通道打包后测试，检查实际 Tk 控件、采集写盘及视频/图表依赖，全部通过后才替换现有 EXE；重新构建前关闭正在运行的 EXE。EXE 和构建产物不进入 Git，仓库保留构建脚本、规格及依赖声明。

## 实时采集

在“实时采集”页选择真实串口，设备参数固定为 921600 baud、8N1，自动识别双通道 v1 / 四通道 v2。连接后可预览，点击“开始记录”创建独立实验目录。先开始电脑记录和外部相机录像，再手动复位 MCU，以录到起始编码。需要结束编码时，先操作 MCU 停止按键并完整录入编码，再停止电脑端记录。四通道须按 [接线说明](../Docs/FOUR_CHANNEL_GUIDE.md) 使用新固件，不能沿用 PC7 DRDY 接线。

四通道保留两板独立时钟，MCU 连续测量 DRDY，客户端每秒更新模型，直接输出公共 1000 Hz 四路表和约延后 0.5 秒的显示。主表为已对齐/重采样值，原生 ADC 码和二进制另存。某板缺样时仅其通道为 NaN 并标记；缺口不被隐藏插值。原生接收、原生保存和四路网格计数分别显示，另显示 A/B 测得采样率、相对 ppm、缓存与模型状态。约 20 秒自动生成固定参考，实际输出继续采用持续模型。

“停止记录”会在后台完成已接收数据的收尾和关闭，它不向 MCU 发送停止命令。断开、异常和写盘失败会保留已有数据与状态报告。采集和离线重型任务互斥，停止记录后先断开预览，再分析视频或载入回放。

真实串口使用独立接收线程持续读取，解析、时钟拟合和写盘仍由一个有序线程完成。应用层接收缓冲最多 256 KiB，不依赖驱动是否接受缓冲大小请求；积压过大、溢出或断线会明确报错并保留记录。记录报告同时保存帧错、原生序号缺口、包序号缺口及接收缓冲诊断，MCU 错误计数为零不能单独证明电脑端没有丢数据。

各路未滤波值、滤波值和 TTL 均保存，四通道的原生码另存 `native_samples.csv`。支持高/低通、50 Hz 陷波及默认关闭的 MAD 去尖峰；可点击“EMG 20–200 Hz”应用频带。在线修改会记录生效会话和网格序号，滤波重新预热期间有质量标记。原生值不经过这些处理；“暂停画面”不暂停记录。

“坐标设置…”提供 1 ms–60 s 的时间窗口、秒/毫秒单位、实时跟随和手动起止时间，以及各通道的自动/手动纵轴。手动范围可按 µV 或 mV 输入，显示单位可独立固定或自动切换；自动模式在可视量程达到 1000 µV 时显示 mV。各路纵轴默认独立，勾选联动后全部通道共用量程，解除后恢复原有独立设置。点击“应用”统一校验；无效输入保留当前视图。采集初期不足窗口时仍显示完整设定范围，例如只有 2 秒数据时，默认横轴仍为 0–10 s。

图内滚轮缩放共享时间轴，左键拖拽平移；按住 Shift 使用滚轮或拖拽调整当前通道纵轴。双击 EMG 图恢复自动纵轴和实时跟随。暂停或操作横轴会固定当时最近 60 秒缓存供查看，后台采集、写盘及统计继续推进；“恢复实时”回到最新数据并保留纵轴设置，“重置坐标”恢复默认 10 秒和独立自动量程。重新连接或检测到 MCU 新会话时，历史副本自动清除。TTL 始终标为 0/1。显示设置仅在本次运行中有效，不写入配置或实验文件。同步回放共享刻度与布局修复，保留原有公共时间与播放操作。

首帧到达前滚动图表只调整跟随窗口，不能冻结空历史；单击图表也不会暂停，实际拖动后才平移。实时页会提示尚未收到样本、已有串口字节但无有效样本，以及超过 2 秒没有新样本的输入停滞。“恢复实时”仅改变显示状态，不会发送 MCU 启动命令。输入停滞时保留已有曲线和后台记录状态；先检查采集计数和控制线连接，PWDN 应接到 PC8。

没有硬件时可选择“模拟输入”或“二进制回放”，在开始记录后推进，界面和清单会标明来源。倍率 1 表示按原速推进，0 表示加速执行。模拟输入重新开始一轮起始编码时，先断开再连接。软件回放不能代表真实串口硬件验收。

## 独立视频分析与标定

“视频分析”页不要求 EMG，可以仅提取 LED、仅追踪反光点，或两者共享解码。LED ROI 使用原始完整帧坐标；提供内参后，反光点 ROI 在去畸变完整帧中选择。没有标定也可输出原始像素诊断，毫米坐标需要相机内参和同一运动平面的单应矩阵。

本实验默认 5 个点。用户提供的 Q12-50-3 标定板为 11×8 个内角点、3.0 mm 方格。商品示意图只确定规格；“相机标定”页要使用实际相机拍摄的图片及棋盘格视频帧。相机、镜头、焦距、分辨率和运动平面改变后需重新标定。

```powershell
python main.py calibrate-intrinsics --images "F:/experiment/calibration/*.jpg" --output F:/experiment/intrinsics.json --cols 11 --rows 8 --square-mm 3
python main.py calibrate-plane --video F:/experiment/video.mp4 --intrinsics F:/experiment/intrinsics.json --frame 0 --output F:/experiment/homography.json --cols 11 --rows 8 --square-mm 3
```

至少需要 3 张可检测棋盘的图片，建议准备多角度、覆盖画面不同区域的清晰照片。内参与平面标定默认分别检查 RMS ≤2 px、≤1 mm。平面单应矩阵不能补偿反光点离开运动平面的误差。

## 时间对齐与同步回放

停止记录后，本次 CSV 自动填入“时间对齐”页。导入对应视频后，可直接运行一键处理，也可选择已完成的“视频分析”结果，校验源视频哈希后复用数据。毫米模式要求有效标定；明确勾选“先验证像素坐标”时可先融合像素轨迹，毫米列留空。仅同步验证模式可用于只有 LED 的样例。

公共零点是起始编码首位变高。程序支持固件先启动 TTL、后启动 ADC 导致 EMG 首个上升沿不可见的情况。至少匹配 10 个稳定边沿，拟合补偿偏移和时钟速率差；默认 RMS 和最大残差均不得超过一个视频帧。没有结束码时允许覆盖区间外的正常尾部差异；覆盖区间内的漏边沿、额外跳变、错误会话和结束码不一致会拒绝。

“同步回放”页载入对齐结果，或点击“载入当前结果”。首次载入校验处理产物及视频并建立磁盘索引，随后可拖动公共时间、播放/暂停、逐帧查看、选择倍率及原始/滤波支路。视频叠加当前帧的检测点，旁表显示五点像素/毫米坐标和状态，各路 EMG 显示同一公共时间竖线。长遮挡后重新分配的 ID 带问号和 `reacquired_unverified`，不会自动视为身份已确认。

## BL-422I 刺激标注与三源回放

“BL 刺激数据”页可独立读取第二通道 TTL CSV，预览电压和事件；不读取 ELE。支持 GB18030、UTF-8/BOM 导出格式，检查采样率、行数、序号、时间和电压单位。`Stimulate: …mA` 作为单脉冲开始的文字标注，保留 0 和负电流设定；未知文字及未知脉宽保留。可逐条填写脉宽/备注，或将同一脉宽应用全部，再另存参数 CSV。原始 BL 文件不修改。

在“时间对齐”页加入同次实验的 BL CSV 和可选参数表，其余 EMG、视频、ROI、标定流程照常。BL 直接与 MCU 的 TTL 匹配，视频单独匹配 MCU；只在三路可信覆盖交集导出主融合表。报告提供真实起始边沿、频差、仿射或分段映射、留一验证残差；缺少起始或存在不可解释的缺边沿时拒绝同步。事件表仍保存覆盖外标注，不向覆盖外推算时间。无 BL 时保留原有双源流程。

新版结果含 `result_manifest.json`、`replay_signals.csv` 及轨迹/帧/事件等处理产物，重新回放无需原始 EMG 和 BL CSV。原视频仍需可访问；移动后点击“重新定位视频”，仅接受内容哈希相同的视频。“打包回放”将视频和必需产物复制到新目录，使用相对路径，可搬移；可选附带原始文件。打包中止不会产生有效完成清单。旧版结果继续通过原索引方式打开。

回放新增紫色事件标线、事件列表、上一/下一刺激及双击跳转。电流显示为设定 mA，脉宽显示 ms，缺失值显示“未知”。这些文字时刻不等于已校准的实际刺激输出时刻，不能据此宣称毫秒级绝对生理潜伏期。

```powershell
python main.py inspect-bl --csv <BL第二通道.csv> --output output/bl_inspection_new
python main.py run --config <配置.json> --bl-csv <BL第二通道.csv> --stimulus-parameters <纸面参数.csv>
python main.py package-replay --result <同步结果目录> --output <新便携目录>
python scripts/validate_bl_replay_scale.py --output output/bl_scale_new
```

索引使用连续时间列和磁盘窗口读取，定位不重复扫描整张 CSV。源文件改变后需重新处理或重建索引。TTL 残差不能量出 ADS 数字滤波、LED、曝光和滚动快门的固定延迟，滤波曲线也没有被自动做相位补偿。

## 实验文件与输出

每次记录的 `session.json` 标识来源和状态，`acquisition/` 保存 `emg.csv`、`serial.bin`、`filter_events.jsonl`、`acquisition_report.json`。四通道另保存 `native_samples.csv`、`metadata.json`、`sync_edges.csv`、`clock_models.jsonl` 和 `clock_reference.json`，请按整个实验目录保管，视频对齐依赖实测边沿台账。未正常结束的记录保留 `emg.partial.csv` 和未完成状态，正式对齐会拒绝把它当作完整记录使用。

独立视频结果包含 `video_analysis_report.json`、TTL 或帧时间表、轨迹及可复用缓存。正式对齐生成 `video_ttl.csv`、`frame_alignment.csv`、`ttl_edge_matches.csv`、`data_gaps.csv`、`alignment_report.json`、`qc_alignment.png`；完整融合另生成 `tracking.csv`、`aligned_by_frame.csv`、`aligned_by_emg_sample.csv`，保留原始及滤波支路。

两种主表保留原始索引和公共时间。默认 EMG 不跨缺样插值，滤波值不跨设置修订边界插值；轨迹不在帧覆盖外外推。30 分钟、1 kSPS、5 点的样本长表约 900 万行，需要较大磁盘空间和导出时间；程序显示估算并支持取消导出，原始采集文件保持不变。

每次派生处理选择新输出目录。结果页可生成 30 帧/150 行人工抽查包，填写 LED、点 ID、像素位置和已知平面距离记录；生成模板不会自动把真实实验验收标为通过。

## 命令行

```powershell
python main.py record --port COM3 --output sessions/experiment_01
python main.py record --simulate --seconds 10 --speed 1 --output sessions/demo_01
python main.py record --simulate --channels 4 --seconds 30 --speed 1 --output sessions/demo_4ch_01
python main.py record --replay sessions/demo_01/acquisition/serial.bin --speed 0 --output sessions/replayed_01
python main.py analyze-video --video experiment.mp4 --output output/led_01 --led-roi 350,850,250,250
python main.py analyze-video --config config.full-example.json --output output/video_01
python main.py run --config config.full-example.json --video-analysis output/video_01 --output output/aligned_01
python main.py index-replay --result output/aligned_01
python main.py review --result output/aligned_01
```

`config.full-example.json` 与 `config.sync-template.json` 中的文件路径和 ROI 要替换成实际数据。仓库不含实验原始数据；本地 `config.sync-example.json` 若存在则优先供“载入同步样例”使用，不随 Git 提交。无效视频时间戳默认拒绝，只有确认固定帧率后才能显式允许 FPS 回退。GUI 导入 JSON 的相对路径相对于配置文件解析，保存配置使用绝对路径。

## 验证与执行状态

```powershell
python -m unittest discover -s tests -v
python main.py validate-integration --output output/integration_validation_new
python -u scripts/validate_acquisition_long.py --output output/acquisition_long_new
python -u scripts/validate_replay_long.py --acquisition output/acquisition_long_new --output output/replay_long_new
python scripts/validate_four_channel_clock.py --seconds 1800 --output output/clock_4ch_new.json
python scripts/validate_four_channel_acquisition.py --seconds 1800 --output output/acquisition_4ch_new
python scripts/validate_four_channel_fusion.py --output output/fusion_4ch_new
python scripts/validate_four_channel_serial.py --port COM5 --seconds 1800 --output output/serial_4ch_new
python scripts/validate_four_channel_analog.py --acquisition output/serial_4ch_new/session/acquisition --output output/analog_4ch_new --min-duration 1800
```

整体软件验证保留合成二进制、后台采集 CSV、独立视频结果、两种融合表、同步回放索引和报告。长采集脚本验证 30 分钟等效的 180 万样本；长回放脚本检查 30 分钟数据的随机定位，使用显式合成几何/时间真值，不等同于真实硬件或 900 万行全量导出验收。

实测操作见 [实验检查清单](../Docs/EXPERIMENT_CHECKLIST.md) 与 [四通道接线说明](../Docs/FOUR_CHANNEL_GUIDE.md)，输入协议见 [输入格式](../Docs/INPUT_FORMAT.md)。四通道新增协议、漂移重采样、缺样/复位、原生二进制重放、视频融合、磁盘回放及四档 Tk 字体比例测试。113项回归及最终EXE真实串口、便携检查已通过；修复接收缓冲后三十分钟真实记录完整，1Hz共同模拟输入边沿观测满足跨板1ms门槛。当前动态接线最终整套断电及60秒复核也已通过；整个20–200Hz模拟频带、真实动物EMG/动作及五点视频毫米尺度仍须由相应实测数据确认。计划、执行状态、测试报告和实验数据保留本地，不随 Git 发布。

可用 `python scripts/preview_axes_ui.py` 打开明确标识的模拟显示窗口；该脚本不连接设备或写入采集文件。运行 `python scripts/validate_axes_layout.py --result <已有对齐结果目录>` 可复核实际实时页、设置窗口和回放页在四档字体缩放下的控件边界。

共同模拟输入验收仅用于无动物的已知同源台架，先按 [TTL 测试说明](../Docs/TTL_TEST_GUIDE.md) 完成电平核对；不要对不同肌肉调用该方法来校正真实激活差异。诊断工具报告50%边沿观测值、噪声估计及原生/固定参考对照，不修改采集文件或时钟模型。
