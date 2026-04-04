# Oracle Cloud Free Tier Deployment Guide

## Overview
Deploy InfiniteTalk bot on Oracle Cloud's Always Free tier using an ARM instance.

---

## Prerequisites
- Oracle Cloud account (free tier)
- Local machine with SSH client
- Your Telegram bot token from @BotFather
- RunPod API credentials

---

## Step 1: Create Oracle Cloud Instance

### 1.1 Sign up for Oracle Cloud Free Tier
1. Go to [cloud.oracle.com](https://cloud.oracle.com)
2. Click "Sign Up" and create a free account
3. You'll get:
   - 2 AMD-based VMs (1 OCPU, 1GB RAM each)
   - 4 ARM-based VMs (1 OCPU, 6GB RAM each) **← Use this**
   - 200GB block storage
   - 10TB object storage

### 1.2 Create ARM Instance (Recommended)
1. Login to Oracle Cloud Console
2. Navigate to **Compute → Instances**
3. Click **Create Instance**
4. Configure:
   - **Name**: `infinitetalk-bot`
   - **Compartment**: Select your compartment
   - **Availability Domain**: Any
   - **Image**: 
     - Click "Edit"
     - Select "Oracle Linux" or "Ubuntu"
     - Choose **ARM (aarch64)** version
   - **Shape**: 
     - Click "Edit"
     - Select **VM.Standard.A1.Flex** (ARM)
     - Set **OCPU count**: 1
     - Set **Memory**: 6GB
   - **SSH Key**: Upload your public SSH key
5. Click **Create Instance**

### 1.3 Alternative: AMD Instance (if ARM unavailable)
- Use **VM.Standard.E2.1.Micro** (AMD)
- 1 OCPU, 1GB RAM
- May need swap file for memory constraints

---

## Step 2: Configure Instance Security

### 2.1 Add Ingress Rules
1. Go to your instance details
2. Click **Virtual Cloud Network → Subnet → Security Lists**
3. Add Ingress Rules:
   - **Port 22** (SSH) - Source: 0.0.0.0/0
   - **Port 8000** (API) - Source: 0.0.0.0/0 (optional, for testing)

### 2.2 Connect via SSH
```bash
# Get public IP from instance details
ssh -i ~/.ssh/your-key opc@<PUBLIC_IP>

# For Ubuntu instances:
ssh -i ~/.ssh/your-key ubuntu@<PUBLIC_IP>
```

---

## Step 3: Install Docker & Dependencies

### 3.1 Update System
```bash
# Oracle Linux
sudo yum update -y

# Ubuntu
sudo apt update && sudo apt upgrade -y
```

### 3.2 Install Docker
```bash
# Oracle Linux
sudo yum install -y docker
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -aG docker opc

# Ubuntu
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker ubuntu
newgrp docker
```

### 3.3 Install Docker Compose
```bash
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### 3.4 Create Swap (for AMD 1GB instances)
```bash
# Only needed for AMD instances with 1GB RAM
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## Step 4: Deploy the Bot

### 4.1 Clone/Upload Project
```bash
# Option 1: If you have it in Git
git clone https://github.com/yourusername/infinitetalk_telegram.git
cd infinitetalk_telegram

# Option 2: Upload from local
# scp -r ./infinitetalk_telegram opc@<IP>:/home/opc/
```

### 4.2 Configure Environment
```bash
# Copy template and edit
cp .env.example .env
nano .env
```

Add your credentials:
```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
INFINITETALK_ENDPOINT_ID=your_runpod_endpoint_id
RUNPOD_API_KEY=your_runpod_api_key
S3_ENDPOINT_URL=https://s3api-eu-ro-1.runpod.io
S3_ACCESS_KEY_ID=your_s3_access_key
S3_SECRET_ACCESS_KEY=your_s3_secret_key
S3_BUCKET_NAME=your_bucket_name
S3_REGION=eu-ro-1

# Optional: Adjust cleanup settings for free tier
VIDEO_RETENTION_SECONDS=1800  # 30 minutes
CLEANUP_INTERVAL_SECONDS=300  # 5 minutes
```

### 4.3 Start Services
```bash
# Build and start
docker compose up -d --build

# Check status
docker compose ps
docker compose logs -f
```

---

## Step 5: Monitor & Maintain

### 5.1 Check Logs
```bash
# API logs
docker compose logs -f api

# Bot logs  
docker compose logs -f bot

# Both services
docker compose logs -f
```

### 5.2 Resource Monitoring
```bash
# Check memory usage
free -h

# Check disk usage
df -h

# Check Docker containers
docker stats
```

### 5.3 Auto-restart on Reboot
```bash
# Docker should auto-start containers
# Verify by rebooting:
sudo reboot

# After reboot, check:
docker compose ps
```

---

## Step 6: Optional Enhancements

### 6.1 Add Custom Domain (Optional)
1. Use Cloudflare (free) to point domain to your Oracle IP
2. Configure SSL with Cloudflare's free SSL

### 6.2 Set Up Monitoring
```bash
# Install htop for better monitoring
sudo yum install htop  # Oracle Linux
sudo apt install htop  # Ubuntu

# Monitor with htop
htop
```

### 6.3 Backup Configuration
```bash
# Backup your .env file
cp .env .env.backup

# Optional: Backup to Oracle Object Storage
# (Free 10GB available)
```

---

## Troubleshooting

### Common Issues

#### 1. Out of Memory (AMD 1GB instance)
```bash
# Check memory usage
free -h

# Add more swap if needed
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

#### 2. Docker won't start
```bash
# Check Docker status
sudo systemctl status docker

# Restart Docker
sudo systemctl restart docker
```

#### 3. Bot not responding
```bash
# Check bot logs
docker compose logs bot

# Check if bot token is correct
# Verify in .env file
```

#### 4. API not accessible
```bash
# Check if port 8000 is open
sudo netstat -tlnp | grep 8000

# Check security list rules in Oracle Console
```

---

## Cost Summary

| Resource | Cost | Notes |
|----------|------|-------|
| ARM Instance | **FREE** | 4x ARM VMs (1 OCPU, 6GB RAM) |
| Storage | **FREE** | 200GB block storage |
| Data Transfer | **FREE** | 10TB/month outbound |
| RunPod GPU | Pay-per-use | Only when generating videos |
| **Total Monthly** | **$0-5** | Only RunPod usage costs |

---

## Performance Tips

### For ARM Instances (Recommended)
- ✅ 6GB RAM handles multiple concurrent jobs
- ✅ ARM architecture is efficient
- ✅ No swap needed

### For AMD Instances (1GB RAM)
- Use swap file (2-4GB)
- Reduce cleanup intervals
- Monitor memory usage closely

---

## Security Notes

1. **Never commit .env to git** (already in .gitignore)
2. **Use SSH keys** (not passwords)
3. **Keep Oracle Console secure** with 2FA
4. **Regularly update** packages
5. **Monitor logs** for unusual activity

---

## Next Steps

1. **Deploy** using this guide
2. **Test** the bot with /start command
3. **Monitor** resources for first few days
4. **Adjust** cleanup settings based on usage

Your InfiniteTalk bot should now be running on Oracle Cloud Free tier! 🎉
