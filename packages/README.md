# packages

当前目录提供可直接安装的 `deb` 包与构建脚本。

## 现成安装包

- `mido-wifi-portal_1.0.0_all.deb`
  已包含 Web 管理面板、GPIO 指示灯服务、风扇服务、ESP 单线服务和默认配置

## 安装

```bash
sudo dpkg -i mido-wifi-portal_1.0.0_all.deb
sudo apt-get -f install
```

## 重新构建

在有 `dpkg-deb` 的 Debian/Ubuntu 环境里执行：

```bash
bash packages/build_mido_wifi_portal_deb.sh
```
