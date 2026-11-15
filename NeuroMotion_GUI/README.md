# NeuroMotion GUI

NeuroMotion GUI 是 NeuroMotion 系统的 Python 桌面客户端，整合 NUCLEO-F446RE + ADS1292R 实时双通道采集、导入视频后的五反光点追踪与像素/毫米坐标换算、LED→TTL 提取、公共时间轴对齐，以及 EMG/视频/位置同步回放。视频录像由外部设备完成。MCU 固件维持原时序，客户端控制电脑端连接和记录。

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

构建临时文件和日志在 `build/windows/`，最终文件为本目录的 `NeuroMotion.exe`。脚本默认运行打包后测试，检查实际窗口、采集写盘及视频/图表依赖；重新构建前关闭正在运行的 EXE。EXE 和构建产物不进入 Git，仓库保留构建脚本、规格及依赖声明。

## 实时采集

在“实时采集”页选择真实串口，设备参数固定为 921600 baud、8N1、两路 1000 Hz。连接后可预览，点击“开始记录”创建独立实验目录。先开始电脑记录和外部相机录像，再手动复位 MCU，以录到起始编码。需要结束编码时，先操作 MCU 停止按键并完整录入编码，再停止电脑端记录。

“停止记录”会在后台完成已接收数据的收尾和关闭，它不向 MCU 发送停止命令。断开、异常和写盘失败会保留已有数据与状态报告。采集和离线重型任务互斥，停止记录后先断开预览，再分析视频或载入回放。

两路原始 μV、滤波 μV 和 TTL 均保存。支持高/低通、50 Hz 陷波及默认关闭的 MAD 去尖峰；在线修改设置会记录生效的会话和样本序号。原始值始终不经过这些处理。界面可切换原始/滤波支路；“暂停画面”不暂停记录。

“坐标设置…”提供 1 ms–60 s 的时间窗口、秒/毫秒单位、实时跟随和手动起止时间，以及各通道的自动/手动纵轴。手动范围可按 µV 或 mV 输入，显示单位可独立固定或自动切换；自动模式在可视量程达到 1000 µV 时显示 mV。两路纵轴默认独立，勾选联动后共用量程，解除后恢复原有独立设置。点击“应用”统一校验；无效输入保留当前视图。采集初期不足窗口时仍显示完整设定范围，例如只有 2 秒数据时，默认横轴仍为 0–10 s。

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

停止记录后，本次 CSV 自动填入“时间对齐”页。导入对应视频后，可直接运行一键处理，也可选择已完成的“视频分析”结果，校验源视频哈希后复用数据。完整融合要求有效毫米坐标；仅同步验证模式可用于当前只有 LED 的真实样例。

公共零点是起始编码首位变高。程序支持固件先启动 TTL、后启动 ADC 导致 EMG 首个上升沿不可见的情况。至少匹配 10 个稳定边沿，拟合补偿偏移和时钟速率差；默认 RMS 和最大残差均不得超过一个视频帧。没有结束码时允许覆盖区间外的正常尾部差异；覆盖区间内的漏边沿、额外跳变、错误会话和结束码不一致会拒绝。

“同步回放”页载入对齐结果，或点击“载入当前结果”。首次载入校验源文件并建立磁盘索引，随后可拖动公共时间、播放/暂停、逐帧查看、选择倍率及原始/滤波支路。视频叠加当前帧的检测点，旁表显示五点像素/毫米坐标和状态，两路 EMG 显示同一公共时间竖线。长遮挡后重新分配的 ID 带问号和 `reacquired_unverified`，不会自动视为身份已确认。

索引使用连续时间列和磁盘窗口读取，定位不重复扫描整张 CSV。源文件改变后需重新处理或重建索引。TTL 残差不能量出 ADS 数字滤波、LED、曝光和滚动快门的固定延迟，滤波曲线也没有被自动做相位补偿。

## 实验文件与输出

每次记录的 `session.json` 标识来源和状态，`acquisition/` 保存 `emg.csv`、`serial.bin`、`filter_events.jsonl`、`acquisition_report.json`。未正常结束的记录保留 `emg.partial.csv` 和未完成状态，正式对齐会拒绝把它当作完整记录使用。

独立视频结果包含 `video_analysis_report.json`、TTL 或帧时间表、轨迹及可复用缓存。正式对齐生成 `video_ttl.csv`、`frame_alignment.csv`、`ttl_edge_matches.csv`、`data_gaps.csv`、`alignment_report.json`、`qc_alignment.png`；完整融合另生成 `tracking.csv`、`aligned_by_frame.csv`、`aligned_by_emg_sample.csv`，保留原始及滤波支路。

两种主表保留原始索引和公共时间。默认 EMG 不跨缺样插值，滤波值不跨设置修订边界插值；轨迹不在帧覆盖外外推。30 分钟、1 kSPS、5 点的样本长表约 900 万行，需要较大磁盘空间和导出时间；程序显示估算并支持取消导出，原始采集文件保持不变。

每次派生处理选择新输出目录。结果页可生成 30 帧/150 行人工抽查包，填写 LED、点 ID、像素位置和已知平面距离记录；生成模板不会自动把真实实验验收标为通过。

## 命令行

```powershell
python main.py record --port COM3 --output sessions/experiment_01
python main.py record --simulate --seconds 10 --speed 1 --output sessions/demo_01
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
```

整体软件验证保留合成二进制、后台采集 CSV、独立视频结果、两种融合表、同步回放索引和报告。长采集脚本验证 30 分钟等效的 180 万样本；长回放脚本检查 30 分钟数据的随机定位，使用显式合成几何/时间真值，不等同于真实硬件或 900 万行全量导出验收。

实测操作见 [实验检查清单](../Docs/EXPERIMENT_CHECKLIST.md)，输入协议见 [输入格式](../Docs/INPUT_FORMAT.md)。计划、执行状态、测试报告、原始实验数据与诊断输出保留在本地，不随 Git 发布；自动化测试源码保留在 `tests/`。真实串口连续 30 分钟、真实五点配对视频及毫米尺度验收仍待实测。

可用 `python scripts/preview_axes_ui.py` 打开明确标识的模拟显示窗口；该脚本不连接设备或写入采集文件。运行 `python scripts/validate_axes_layout.py --result <已有对齐结果目录>` 可复核实际实时页、设置窗口和回放页在四档字体缩放下的控件边界。
