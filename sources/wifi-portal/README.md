# Mido WiFi Portal

这是给无头 `mido` 开发板准备的一套本地配网门户。

目标：

- 无外网时自动拉起 Wi-Fi 热点
- 提供本地 Web 配网页
- 提供系统控制台与 GPIO 调试页
- 允许保存 Wi-Fi，联网成功后自动退出热点
- 面板风格接近路由器管理页，适合手机访问

## 目录

- `mido_wifi_portal.py`
  主服务，包含 Web UI 与联网/热点守护逻辑
- `mido-wifi-portal.service`
  systemd 服务单元
- `config.example.json`
  默认配置样例
- `install.sh`
  安装脚本，部署到当前系统

## 安装

```bash
cd wifi-portal
sudo bash install.sh
```

安装脚本会自动补齐运行依赖：

- `python3`
- `network-manager`
- `dnsmasq-base`
- `iw`
- `iputils-ping`
- `gpiod`
- `python3-libgpiod`

安装完成后服务会自动启动：

```bash
systemctl status mido-wifi-portal.service
```

## 默认行为

- 正常联网时：
  Web 面板通过当前 IP 提供服务
- 连续检测不到可用外网时：
  自动尝试启用热点
- 用户通过面板提交新的 Wi-Fi 后：
  自动关闭热点并连接目标网络
- 常见探测路径如 `/generate_204`、`/hotspot-detect.html` 会回跳到门户首页，便于后续做配网入口

## 默认热点

- SSID: `mido-setup`
- Password: `12345678`

可以在 `/etc/mido-wifi-portal/config.json` 修改。
