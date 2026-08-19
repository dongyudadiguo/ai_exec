# termux_responses

`responses` 的 Termux / Android 版本。

官方 `psutil` 不支持 Android（`platform android is not supported`），本目录用同级的精简 `psutil.py` 代替，只实现 `viewer.py` 用到的 PID / 进程树接口。

## 安装

在 Termux 里：

```bash
pkg update
pkg install python
pip install flask requests
```

不要执行 `pip install psutil`。

## 运行

把本目录拷到手机后：

```bash
cd ~/ai_exec/termux_responses   # 按实际路径改
python viewer.py
```

终端会打印：

```text
input.json viewer: http://127.0.0.1:8765
Termux: termux-open-url http://127.0.0.1:8765
```

用手机浏览器打开该地址。装了 Termux:API 也可以：

```bash
termux-open-url http://127.0.0.1:8765
```

或直接：

```bash
bash start.sh
```

## 局域网访问

默认只监听本机。若要从电脑访问手机上的 viewer：

```bash
AE_VIEWER_HOST=0.0.0.0 python viewer.py
```

端口可用 `AE_VIEWER_PORT` 覆盖。

## 注意

- 本目录的 `psutil.py` 仅给 Termux 用，不要拷到 Windows 的 `responses/` 里覆盖真实 psutil。
- Windows 请继续用原来的 `responses/` + `pip install flask psutil requests`。
