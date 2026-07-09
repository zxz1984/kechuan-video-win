# 可乐 AI 分析网页版 - 部署到 1.12.50.45

## 服务器要求
- 腾讯云 4核4G 50G 5M
- 系统：Ubuntu 22.04 / Debian 11+
- **绝对避开端口 3011**（那是另一个软件，本服务用 8080）

## 部署步骤

### 1. SSH 到服务器
```bash
ssh -i ~/Desktop/zxz.pem root@1.12.50.45
```

### 2. 装依赖
```bash
apt update
apt install -y python3.13 python3-pip ffmpeg git
```

### 3. 推代码（任选一种）

**方式 A：scp 推整个项目**
```bash
# 在本地（Mac）执行：
scp -i ~/Desktop/zxz.pem -r \
    "/Users/zxz/Desktop/可乐剪辑/可乐视频生成器_Mac版_v1.0" \
    root@1.12.50.45:/opt/kele-web/
```

**方式 B：git（如果项目有仓库）**
```bash
# 在服务器上：
cd /opt
git clone <你的仓库> kele-web
cd kele-web
```

### 4. 装 Python 包
```bash
cd /opt/kele-web
python3.13 -m pip install --break-system-packages -r web/requirements-web.txt
```

### 5. 改 config.yaml（如需要）
```bash
nano config.yaml
# 确认 asr.key / llm.key / llm.url 都对
```

### 6. 启动
```bash
chmod +x web/start.sh
./web/start.sh prod
```

启动后输出：
```
🚀 生产模式启动 (gunicorn): http://0.0.0.0:8080
```

### 7. 测试
```bash
curl http://127.0.0.1:8080/health
# 应该返回：{"status":"ok","tasks":0,...}
```

浏览器打开 `http://1.12.50.45:8080` 应该看到上传页面。

## 用 systemd 守护进程（可选）

让服务开机自启 + 异常自动重启：

```bash
cat > /etc/systemd/system/kele-web.service << 'EOF'
[Unit]
Description=可乐 AI 分析网页版
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/kele-web
ExecStart=/usr/bin/python3.13 -m gunicorn \
  --workers 2 --threads 2 \
  --bind 0.0.0.0:8080 \
  --timeout 600 \
  --access-logfile /tmp/web_access.log \
  --error-logfile /tmp/web_error.log \
  web.server:app
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable kele-web
systemctl start kele-web
systemctl status kele-web
```

## 环境变量（可选）

| 变量 | 默认 | 说明 |
|------|------|------|
| `WEB_PORT` | 8080 | 监听端口 |
| `WEB_HOST` | 0.0.0.0 | 监听地址 |
| `WEB_JOB_DIR` | /tmp/web_jobs | 临时文件目录 |
| `WEB_TASK_TTL_MIN` | 10 | 任务自动清理（分钟） |

```bash
export WEB_PORT=8080
export WEB_TASK_TTL_MIN=10
./web/start.sh prod
```

## 反向代理（Nginx，可选）

如果想用 80/443 端口：

```nginx
server {
    listen 80;
    server_name your-domain.com;

    client_max_body_size 200M;   # 上传视频大小限制

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 600s;  # 长任务超时
    }
}
```

## 监控

```bash
# 实时日志
tail -f /tmp/web_error.log

# 任务目录
ls -lh /tmp/web_jobs/

# 健康检查
curl http://1.12.50.45:8080/health
```

## 备份当前进程

如果改代码：
1. SSH 到服务器
2. `systemctl stop kele-web`
3. 重 scp 推代码
4. `systemctl start kele-web`

或者用 git pull：
```bash
cd /opt/kele-web && git pull && systemctl restart kele-web
```

## 已知限制

- 单 worker 2 线程，能同时处理约 4 个任务
- LLM 中转 `http://14.116.211.42:10003` 必须可达（如果用阿里云部署则需要公网可达）
- 临时文件 10 分钟自动清，不用担心磁盘堆积
- 不支持多用户（自用），后续如果要多人用需加账号系统