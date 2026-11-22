# Zxic Wi-Fi eSIM Manager

中兴随身 Wi-Fi 的局域网管理面板，支持短信和 eSIM 管理。面板通过设备原厂后台管理短信，使用 [euicc-go](https://github.com/damonto/euicc-go) 和设备的 AT 接口管理 eSIM。

## 功能

- 查看、发送、删除短信，并将新收到的短信转发至 Telegram。
- 查看 EID、eSIM 配置及剩余空间，下载、启用、停用、重命名和删除配置；后台会重试未送达的运营商通知。
- 管理无线热点、数据连接和支持的设备指示灯，支持设备备注与界面明暗模式。
- 按 USB 端口分别访问设备，支持原厂后台的 `/goform` 和 `/reqproc` 路径。

设备接入后，面板会关闭无线热点和可控制的指示灯，并在两分钟后关闭数据连接；之后可以在面板中重新开启。重启面板服务不会重新关闭同一次接入期间手动开启的设置。指示灯控制需要设备提供 ADB 和可写的 Linux LED 节点。

## 部署

需要 Linux、Python 3、systemd-networkd、libusb、Go 1.26.3 或更高版本，以及用于指示灯控制的 `adb`。设备自动发现依赖 systemd-networkd 的 DHCP 租约。以下步骤使用 `/opt/zxw-panel` 安装程序：

```sh
cd lpahelper
CGO_ENABLED=0 go build -o ../zxw-lpa-helper .
cd ..
install -d /opt/zxw-panel
cp app.py esim.py esim_transport.py led.py zxw-lpa-helper /opt/zxw-panel/
cp -r web /opt/zxw-panel/
install -d -m 700 /var/lib/zxw-panel
umask 077
openssl rand -hex 32 > /var/lib/zxw-panel/password
install -m 644 deploy/zxw-panel.service /etc/systemd/system/
install -m 644 deploy/05-zxw.network /etc/systemd/network/
systemctl daemon-reload
systemctl restart systemd-networkd
systemctl enable --now zxw-panel.service
```

安装步骤需要相应的系统管理员权限。`05-zxw.network` 会让匹配的中兴 CDC Ethernet 设备通过 DHCP 获取地址，但不接受其默认路由和 DNS；请根据系统的网络管理方式和设备实际情况调整，避免影响其他连接。

面板默认只监听 `127.0.0.1:9528`，使用 HTTP Basic 认证，用户名为 `admin`，密码保存在 `/var/lib/zxw-panel/password`。若需从其他机器访问，请自行配置 HTTPS 反向代理和访问控制。面板服务以 root 运行以访问 USB，能读取短信、控制网络和修改 eSIM 配置，请只允许可信用户访问。

原厂设备后台密码默认为 `admin`；如果设备密码不同，可通过服务环境变量 `ZXW_MODEM_PASSWORD` 修改。当前所有设备共用该设置。Telegram Bot Token 保存在本机数据库 `/var/lib/zxw-panel/panel.sqlite3`，请保护好数据目录及其备份，不要上传密码、数据库、证书或私钥。

在面板填写 Telegram Bot Token 和 Chat ID 后，首次读取会记录设备已有短信，之后的新短信会转发；发送失败的消息会重试。Telegram 只用于发送通知。

## 兼容性

已测试的 `19d2:0536` 设备通过 USB 接口 3 访问 eUICC，`19d2:0581` 使用接口 4，两者使用三参数 `AT+CSIM` 格式。不同固件、卡片和设备可能有不同的接口或行为。删除 eSIM 配置可能无法撤销，请先确认卡片和运营商条件。

## 致谢

- [Sigmo](https://github.com/damonto/sigmo)：本项目的 UI 仿照 Sigmo 设计和实现。
- [euicc-go](https://github.com/damonto/euicc-go)：提供 eSIM 协议实现。
- [wwan-go](https://github.com/damonto/wwan-go)：euicc-go 的依赖。

## 许可证

本项目采用 [MIT License](LICENSE)。第三方组件的许可证保留在 `lpahelper/licenses/`。
