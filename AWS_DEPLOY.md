# AWS Free Tier Deployment Guide

## Overview
Deploy InfiniteTalk bot on AWS Free Tier using EC2 instance.

---

## Prerequisites
- AWS account (free tier eligible)
- Local machine with SSH client
- Your Telegram bot token from @BotFather
- RunPod API credentials

---

## Step 1: Create AWS Account & Free Tier

### 1.1 Sign up for AWS Free Tier
1. Go to [aws.amazon.com/free](https://aws.amazon.com/free)
2. Click "Create a Free Account"
3. You'll get:
   - **750 hours/month** of EC2 t2.micro or t3.micro (1 vCPU, 1GB RAM)
   - **5GB** S3 storage
   - **100GB** EBS storage
   - **15GB** data transfer out

### 1.2 Verify Free Tier Eligibility
- Make sure your account is less than 12 months old
- Or use AWS Free Tier always-eligible services

---

## Step 2: Launch EC2 Instance

### 2.1 Navigate to EC2 Console
1. Login to AWS Console
2. Go to **Services → EC2**
3. Click **Launch Instances**

### 2.2 Configure Instance
1. **Name**: `infinitetalk-bot`
2. **AMI (Amazon Machine Image)**:
   - Select **Ubuntu Server**
   - Choose **Ubuntu 22.04 LTS** (or latest LTS)
   - Architecture: **64-bit (x86)**
3. **Instance Type**:
   - Select **t2.micro** (Free Tier eligible)
   - 1 vCPU, 1GB RAM
4. **Key Pair**:
   - Create a new key pair
   - Name: `infinitetalk-key`
   - Download the `.pem` file (save it!)
5. **Security Group**:
   - Create new security group
   - Name: `infinitetalk-sg`
   - Add rules:
     - **SSH (22)** - Source: 0.0.0.0/0
     - **HTTP (80)** - Source: 0.0.0.0/0 (optional, for testing)
     - **Custom TCP (8000)** - Source: 0.0.0.0/0 (for API)

### 2.3 Storage Configuration
- **Root volume**: 20GB (free tier includes 30GB)
- No additional volumes needed

### 2.4 Launch Instance
1. Review configuration
2. Click **Launch Instance**
3. Wait for instance to initialize (2-3 minutes)

---

## Step 3: Connect to Instance

### 3.1 Get Public IP
1. Go to EC2 Instances
2. Select your instance
3. Copy the **Public IPv4 address**

### 3.2 Connect via SSH
```bash
# Make sure your .pem file has correct permissions
chmod 400 infinitetalk-key.pem

# Connect to instance
ssh -i infinitetalk-key.pem ubuntu@<PUBLIC_IP>
```

---

## Step 4: Setup Instance

### 4.1 Update System
```bash
sudo apt update && sudo apt upgrade -y
```

### 4.2 Install Docker
```bash
# Install Docker
curl -fsSL https://get.docker.com | sh

# Add user to docker group
sudo usermod -aG docker ubuntu

# Activate docker group (logout and login or use newgrp)
newgrp docker
```

### 4.3 Install Docker Compose
```bash
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose
```

### 4.4 Create Swap File (Important for 1GB RAM)
```bash
# Create 2GB swap file
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make swap permanent
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# Verify swap
free -h
```

### 4.5 Install Additional Dependencies
```bash
sudo apt install -y git curl wget htop
```

---

## Step 5: Deploy the Bot

### 5.1 Clone/Upload Project
```bash
# Option 1: If you have it in Git
git clone https://github.com/yourusername/infinitetalk_telegram.git
cd infinitetalk_telegram

# Option 2: Upload from local (use scp)
# scp -r -i infinitetalk-key.pem ./infinitetalk_telegram ubuntu@<IP>:/home/ubuntu/
```

### 5.2 Configure Environment
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

# AWS-specific: Adjust cleanup for 1GB RAM
VIDEO_RETENTION_SECONDS=1800  # 30 minutes
CLEANUP_INTERVAL_SECONDS=300  # 5 minutes
```

### 5.3 Start Services
```bash
# Build and start
docker compose up -d --build

# Check status
docker compose ps
docker compose logs -f
```

---

## Step 6: Monitor & Maintain

### 6.1 Check Logs
```bash
# API logs
docker compose logs -f api

# Bot logs  
docker compose logs -f bot

# Both services
docker compose logs -f
```

### 6.2 Resource Monitoring
```bash
# Check memory usage
free -h

# Check disk usage
df -h

# Check system load
htop

# Check Docker containers
docker stats
```

### 6.3 Auto-restart on Reboot
```bash
# Enable Docker to start on boot
sudo systemctl enable docker

# Docker should auto-restart containers
# Verify by rebooting:
sudo reboot

# After reboot, check:
docker compose ps
```

---

## Step 7: Optional Enhancements

### 7.1 Set up Elastic IP (Free)
1. Go to **EC2 → Elastic IPs**
2. Click **Allocate Elastic IP**
3. Associate with your instance
4. This gives you a static IP

### 7.2 Use AWS S3 for Backups
```bash
# Install AWS CLI
sudo apt install awscli

# Configure (use your AWS credentials)
aws configure

# Backup .env file to S3
aws s3 cp .env s3://your-backup-bucket/.env.backup
```

### 7.3 Set up CloudWatch Monitoring
```bash
# Install CloudWatch agent
sudo apt install amazon-cloudwatch-agent

# Configure basic monitoring
# (Advanced - optional)
```

### 7.4 Domain with Route 53
1. Buy domain or use existing
2. Create Route 53 hosted zone
3. Point A record to your Elastic IP
4. Configure SSL with AWS Certificate Manager

---

## Step 8: Security Hardening

### 8.1 SSH Security
```bash
# Edit SSH config
sudo nano /etc/ssh/sshd_config

# Recommended changes:
# PasswordAuthentication no
# PermitRootLogin no
# Port 2222 (optional, change from 22)

# Restart SSH
sudo systemctl restart ssh
```

### 8.2 Firewall Setup
```bash
# Enable UFW firewall
sudo ufw enable

# Allow SSH
sudo ufw allow ssh

# Allow HTTP/HTTPS (if needed)
sudo ufw allow 80
sudo ufw allow 443

# Check status
sudo ufw status
```

### 8.3 Auto Updates
```bash
# Install unattended-upgrades
sudo apt install unattended-upgrades

# Configure
sudo dpkg-reconfigure -plow unattended-upgrades
```

---

## Troubleshooting

### Common Issues

#### 1. Out of Memory (1GB instance)
```bash
# Check memory usage
free -h

# Check swap
swapon --show

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

# Check logs
sudo journalctl -u docker
```

#### 3. Bot not responding
```bash
# Check bot logs
docker compose logs bot

# Check if bot token is correct
# Verify in .env file

# Restart bot
docker compose restart bot
```

#### 4. Instance not accessible
```bash
# Check security group rules in AWS Console
# Ensure SSH (port 22) is allowed
# Check if instance is running

# Try connecting again
ssh -i infinitetalk-key.pem ubuntu@<PUBLIC_IP>
```

#### 5. Disk space full
```bash
# Check disk usage
df -h

# Clean Docker
docker system prune -a

# Clean old logs
sudo journalctl --vacuum-time=7d
```

---

## Cost Summary

| Resource | Free Tier Limit | Cost After Free Tier |
|----------|-----------------|---------------------|
| EC2 t2.micro | 750 hours/month | ~$8.50/month |
| EBS Storage | 30GB | ~$3/month for 30GB |
| Data Transfer | 15GB/month | ~$0.09/GB after |
| S3 Storage | 5GB | ~$0.023/GB/month |
| **Total First Year** | **FREE** | ~$11.50/month |

---

## Performance Tips for t2.micro

### Memory Management
- ✅ Use 2GB swap file (configured above)
- ✅ Reduce video retention time
- ✅ Monitor with `htop`

### CPU Credits
- t2.micro uses CPU burst credits
- Monitor credit balance in AWS Console
- Consider t3.micro if CPU credits run low

### Storage Optimization
- Regular Docker cleanup: `docker system prune`
- Keep only essential files
- Use AWS S3 for backups

---

## Migration Path

### When to Upgrade
- **Consistent high memory usage** (>80%)
- **Frequent CPU credit depletion**
- **Need for more storage**

### Upgrade Options
1. **t3.micro** - Better performance, same price
2. **t3.small** - 2 vCPU, 2GB RAM (~$17/month)
3. **t3.medium** - 2 vCPU, 4GB RAM (~$34/month)

---

## Backup Strategy

### 1. Code Backup
```bash
# Push to Git regularly
git add .
git commit -m "Backup"
git push origin main
```

### 2. Configuration Backup
```bash
# Backup .env to S3
aws s3 cp .env s3://your-backup-bucket/.env.backup

# Backup docker-compose.yml
aws s3 cp docker-compose.yml s3://your-backup-bucket/docker-compose.yml
```

### 3. Instance Backup
- Create AMI (Amazon Machine Image)
- Schedule regular snapshots
- Store in different region for disaster recovery

---

## Next Steps

1. **Deploy** using this guide
2. **Test** the bot with /start command
3. **Monitor** resources for first week
4. **Set up** alerts for high memory/CPU usage
5. **Configure** backups

Your InfiniteTalk bot should now be running on AWS Free tier! 🎉

---

## Emergency Recovery

If something goes wrong:
```bash
# SSH into instance
ssh -i infinitetalk-key.pem ubuntu@<IP>

# Restart services
docker compose restart

# Check logs
docker compose logs

# Rebuild if needed
docker compose down
docker compose up -d --build
```
