# Telegram Mega Downloader & Channel Forwarder Bot

## Features
1. **Mega Downloader** - Download files from Mega.nz and upload to Telegram channel as photos/videos
2. **Channel Forwarder** - Copy posts between channels without "Forwarded from" tag with custom captions

## Setup on AWS EC2

### 1. Launch EC2 Instance
- Ubuntu 22.04 LTS (t2.micro for small use, t2.medium for heavy use)
- Open port 22 (SSH) in security group
- At least 20GB storage

### 2. Connect & Install Dependencies
```bash
ssh -i your-key.pem ubuntu@your-ec2-ip

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python 3.11+
sudo apt install python3.11 python3.11-venv python3-pip -y

# Clone or upload your bot files
mkdir telegram-bot && cd telegram-bot
# Upload files here

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install requirements
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
nano .env
```

Fill in:
- `API_ID` and `API_HASH` from https://my.telegram.org
- `BOT_TOKEN` from @BotFather

### 4. Run with systemd (keeps running after SSH disconnect)
```bash
sudo nano /etc/systemd/system/telegram-bot.service
```

Paste:
```ini
[Unit]
Description=Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/telegram-bot
ExecStart=/home/ubuntu/telegram-bot/venv/bin/python bot.py
Restart=always
RestartSec=10
EnvironmentFile=/home/ubuntu/telegram-bot/.env

[Install]
WantedBy=multi-user.target
```

Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot

# Check status
sudo systemctl status telegram-bot

# View logs
journalctl -u telegram-bot -f
```

## Bot Commands
| Command | Description |
|---------|-------------|
| /start | Show welcome message |
| /help | Show usage guide |
| /setchannel | Set target channel for Mega uploads |
| /mega | Download from Mega link and upload to channel |
| /forward | Copy posts between channels |

## Important Notes
- **Pyrogram** uses MTProto protocol, so upload limit is **2GB** (not 50MB like Bot API)
- Files > 200MB from Mega will be skipped
- Forward feature removes "Forwarded from" tag by re-sending via file_id
- Bot handles FloodWait automatically
- For Mega transfer limits, you may need to use a VPN/proxy on the EC2 instance

## Mega Transfer Limits
If Mega limits your IP, you can:
1. Use a VPN on EC2: `sudo apt install openvpn` + config
2. Rotate IPs using AWS Elastic IPs
3. Wait for the limit to reset (usually 6 hours)
