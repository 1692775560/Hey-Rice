# Galbot VLA Tree 控制平台

基于 `vla_tree` 目录生成的 Web 控制平台，默认读取同目录导航配置，并兼容缺少真机依赖时的模拟模式。

## 使用的文件

- `../navigation_interface.py`：真机导航接口，后端会优先调用 `get_navigation_interface()`、`navigate_to_pose()`、`stop_navigation()`。
- `../config/location_map.json`：导航预设点位。
- `/var/maps/cur/global_cloud_cleaned.pcd`：默认全局点云地图。
- `server.py`：Flask 后端和 PCD 解析、路径迁移逻辑。
- `static/index.html`：完整控制台；关节、末端、夹爪、传感器、VLA 和自主导航均在同一页面。自主导航的 2D/3D 点云、可达边界、障碍轮廓、朝向选择、主视角和多机协同已原生整合，不使用 iframe。
- `static/navigation.html`：保留的导航独立版来源备份，运行时不再加载。
- `static/vendor/three.module.min.js`：本地 Three.js 资源，用于离线 3D 点云视图。

## 启动

```bash
tar -xzf galbot_control_platform_20260702.tar.gz
cd galbot_control_platform_20260702/control_platform
python3 -m pip install -r requirements.txt
./start.sh
```

然后打开：

```text
http://localhost:7860
```

也可以指定端口：

```bash
./start.sh 8080
```

## 路径覆盖

```bash
export GALBOT_LOCATION_MAP=/path/to/location_map.json
export GALBOT_GLOBAL_MAP=/path/to/global_cloud_cleaned.pcd
export GALBOT_MAIN_CAMERA_SNAPSHOT=/path/to/front_head_camera.jpg
export GALBOT_AGENT_TOKEN=<fleet-agent-token>
export GALBOT_CONTROL_TOKEN=<single-robot-control-token>
```

`GALBOT_MAIN_CAMERA_SNAPSHOT` 可指向一个由机器人相机进程持续更新的 JPG/PNG 文件。未设置时，界面会显示模拟主视角占位图。

设置 `GALBOT_CONTROL_TOKEN` 后，浏览器使用
`http://机器人IP:7861/?token=<single-robot-control-token>` 访问。群控中心使用独立的
`GALBOT_AGENT_TOKEN`，两种 Token 不应相同。

## 控制交互

- URDF 可通过上传按钮或直接拖入“URDF 数字孪生”画布导入。
- 拖动画布关节点会同步更新关节滑块；默认仅预览，点击“同步到真机”后才执行。
- “低速实时跟随”以 `350 ms` 节流，速度不超过 `0.05 rad/s`；普通同步不超过
  `0.12 rad/s`，单次关节变化不超过 `0.25 rad`，并同时检查 URDF 限位和
  `±3.2 rad` 硬限位。
- 末端 IK 与直接末端移动启用 SDK 碰撞检查。
- 地图点击一次选择目标点。
- 点击“选择朝向”后，再在地图上点击目标希望面向的方向，目标 yaw 会同步更新。
- “使用全局地图”关闭后，地图会退出全局 PCD 模式。
- “局部探索建图”会关闭全局地图，从机器人当前位置开始探索边界并创建局部地图。
- 右侧保留主视角摄像头、World Model Context 和 Multi-Robot Coordination 区域，方便后续接世界模型和多机协同。
- 点击“3D”可切换到 3D 点云视图，显示点云高度、探索边界、可达 frontier 边界和障碍物轮廓。

## 官方建图与重定位

自主导航页已接入银河通用官方工具：

- “开始建图”启动 `/data/galbot/bin/mapping_server`。
- W/A/S/D 控制底盘平移，Q/E 控制旋转；松键或通信超过 0.35 秒会自动停车。
- 界面持续显示 `keyframe num`、`time delay` 和建图位姿，并读取底盘 LiDAR 更新点云边界。
- “保存并转导航”先向 `/data/galbot/bin/engine_tools` 输入 `1` 保存地图，确认成功后才关闭建图程序。
- 保存完成后自动加载新 PCD，并使用建图末位姿执行重定位，之后可以直接在地图上选择目标导航。
- “自主探索”不加载或校准已有 PCD，而是清空当前显示、启动新的官方建图会话，再按实时 frontier 探索。
- 地图中的机器人图标以约 `4 Hz` 读取导航位姿，实时更新位置与朝向。
- 点击已探索自由区可直接发送导航目标；“仅显示环境边界”模式隐藏密集点云，只保留实时框线、障碍轮廓、机器人和路径。
- 脱困支持前、后、左、右四个基座方向和 `0.05-1.00 m` 距离，会切换到 `CHASSIS_POSE_CTRL` 并自动停止，不销毁 SDK 连接。

实时探索地图采用后端空间建图流水线：

- 兼容列表点云和二进制 `PointCloud2`，按字段偏移解码 XYZ。
- 使用机器人全局位姿将 LiDAR 局部点转换到地图坐标系。
- 建图仅保留基座坐标高度 `-0.10 m` 至 `0.50 m` 的点；可通过
  `GALBOT_MAP_MIN_HEIGHT` 和 `GALBOT_MAP_MAX_HEIGHT` 调整。
- 使用 `0.08 m` 体素降采样抑制重复点。
- 使用 `0.20 m` 占据栅格和 Bresenham 射线更新自由区、占据区与未知区。
- 障碍需要连续两帧命中才进入占据层；后续自由射线会降低占据证据并清除动态残影。
- 环境框线仅使用机器人连通自由区，障碍轮廓会过滤单栅格孤立噪声。
- 建图过程中固定使用 `mapping_server` 输出的地图位姿；只有缺失时才回退导航位姿，避免坐标系叠图。
- 只在机器人可达自由区上提取并聚类 frontier，封闭障碍后的未知区不会被选为目标。
- 后端持久累积点云，前端不再随机生成探索点。

地图操作：

- 鼠标滚轮或右上角 `+/-` 缩放。
- 按住 Shift 加左键拖动，或使用鼠标中键/右键拖动平移。
- 点击“适配”恢复完整空间范围。
- 点击“擦除”后可在 2D 地图拖动画笔移除误障碍；画笔修改在当前建图会话内保持有效。
- 点击右上角 PCD 下载按钮可导出编辑后的 `edited_global_cloud.pcd`。
- 点击右侧已有预设点位，会同步显示其 X/Y/Yaw 并设置为当前导航目标。
- 在地图上点击位置并选择朝向后，输入点位名称并点击“保存当前目标”，可直接追加到 `config/location_map.json`；列表会立即刷新。
- 每个预设点位右侧提供删除按钮，确认后从 `location_map.json` 原子删除；删除当前目标时会同时清除地图目标和路径。

探索目标使用多目标效用排序，不再仅按最近距离选择：

- BFS 栅格路径代价，避免隔墙直线距离误判。
- 未知区域信息增益和 frontier 聚类规模。
- 障碍物净空与机器人转向代价。
- 已访问区域、重复探索方向的覆盖惩罚。
- 当前目标滞回以及导航超时 frontier 的两分钟失败黑名单。

默认保存目录是 `/var/maps/room1102`，可通过以下环境变量调整：

```bash
export GALBOT_MAPPING_SERVER=/data/galbot/bin/mapping_server
export GALBOT_ENGINE_TOOLS=/data/galbot/bin/engine_tools
export GALBOT_MAPPING_SAVE_PATH=/var/maps/room1102
```

软件遥控和软件急停不能替代机器人实体急停。建图时建议将线速度保持在
`0.2 m/s`、角速度保持在 `0.4 rad/s`，并避免突然反向。

## 传感器监控

- 左臂 RGB：读取 `LEFT_ARM_CAMERA` 压缩帧。
- 左臂深度：解码 `LEFT_ARM_DEPTH_CAMERA` 的 `16UC1` 数据并显示距离热力图。
- 底盘雷达：将 `BASE_LIDAR` 的 `PointCloud2` 规范化为 XYZ 后实时绘制。
- 躯干 IMU：显示 `TORSO_IMU` 的加速度、角速度和磁场三轴值。
- 导航页主视角读取 `HEAD_LEFT_CAMERA`。
- 主视角默认关闭，点击“开启视角”后才请求图像，减少 SDK 和 HTTP 线程占用。

## 资源监控

右下角“机器人资源”窗口每 3 秒读取一次 Linux `/proc`，显示 CPU、内存、
系统负载及高内存进程。默认只有 `mapping_server` 和 `engine_tools` 提供停止按钮，
停止操作发送 `SIGTERM`。可通过以下变量扩展白名单：

```bash
export GALBOT_STOPPABLE_PROCESSES=mapping_server,engine_tools,my_safe_worker
export GALBOT_HTTP_THREADS=8
```

机器人驱动、DDS、定位和底层控制进程默认只读，不允许从网页停止。

## 地图和探索修复点

- PCD 加载后会使用真实 `bounds` 重新计算坐标映射，不再固定在局部 `[-3,3]` 范围。
- 点云导入后会立即对采样点做凸包边界计算，解决“点云加载但边界不更新”的问题。
- 自主探索会先截取可达 frontier 边界，避开障碍轮廓，再选择探索目标。
- 障碍物会按点云密度/高度切成栅格连通域，并用轮廓显示形状。
- 每轮探索后会追加点云采样、重新 fit bounds、重新计算障碍轮廓、可达边界和 3D 点云。
- 缺少真机依赖时，导航接口自动降级为模拟模式，仍可验证 PCD、边界和探索 UI。

## 多机协同

每台机器人都启动本控制平台后，在右侧填写三台机器人的 IP 或 URL，例如：

```text
192.168.1.11:7861
192.168.1.12:7861
192.168.1.13:7861
```

功能：

- “连接三机”：登记三台机器人地址和角色。
- “心跳检测”：通过 `/api/status` 检查在线状态。
- “协同执行”：通过 `/api/fleet/task` 向其它机器人广播协同任务，消息会带上当前目标、地图摘要、障碍数量和可达边界数量。

多机通信当前使用 HTTP JSON，适合先做任务分配、状态同步和地图摘要交换；后续如果需要低延迟共享完整点云，可在此接口下扩展 WebSocket/DDS bridge。
