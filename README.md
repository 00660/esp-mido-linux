# esp-mido-linux

给 Redmi Note 4X (`mido`) 无头 Linux/开发板玩法整理的一套仓库。

当前仓库包含：

- 已验证可刷的镜像集合
- `ESP8266` 电源键/单线控制固件源码
- `mido` 本地 Web 管理面板源码
- 可复用的 `deb` 安装包产物与构建脚本

## 目录

- `images/`
  刷机镜像与校验文件
- `flash-tools/`
  Windows 刷机脚本
- `sources/esp8266_powerkey_ota/`
  `ESP8266` 固件源码
- `sources/wifi-portal/`
  `mido` Web 管理面板源码
- `packages/`
  `deb` 安装包与构建脚本

## 当前镜像

- `images/08-all-except-reserved-bootfs-simg.img`
  你当前确认可用的 `8` 号 `bootfs`
- `images/lk2nd.img`
  当前搭配使用的 `lk`
- `images/rootfs-simg.img.zip`
  当前刷机用 `rootfs` 压缩包，解压后得到 `rootfs-simg.img`

## 安装 Web 面板

如果已经刷好镜像，只想补装管理面板，优先直接装：

```bash
sudo dpkg -i packages/mido-wifi-portal_1.0.0_all.deb
sudo apt-get -f install
```

## 说明

大体积镜像和安装包通过 Git LFS 管理。
