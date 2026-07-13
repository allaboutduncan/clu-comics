# Windows/WSL Setup Guide

## The Problem

When deploying this Docker container on Windows/WSL, you may encounter permission errors like:
```
[Errno 13] Permission denied: '/downloads/temp/filename.cbr.0.crdownload'
```

This happens because:
1. **Windows filesystems don't support Unix ownership** - you can't `chown` a Windows NTFS drive
2. **The container user doesn't match your Windows user ID**
3. **The default PUID=99, PGID=100 doesn't work with Windows volumes**

## The Solution

You need to set the `PUID` and `PGID` environment variables to match your Windows user ID.

### Step 1: Find Your Windows User ID

In WSL, run this command to find your user ID:
```bash
id -u $USER
```

Example output:
```bash
$ id -u $USER
1000
```

### Step 2: Find Your Windows Group ID

In WSL, run this command to find your group ID:
```bash
id -g $USER
```

Example output:
```bash
$ id -g $USER
1000
```

### Step 3: Update Your Portainer Configuration

In Portainer, add these environment variables to your container:

```yaml
environment:
  - PUID=1000    # Replace with your actual user ID
  - PGID=1000    # Replace with your actual group ID
  - UMASK=000
  - FLASK_ENV=development
  - MONITOR=no
```

### Step 4: Alternative - Update Your Docker Compose File

If using Docker Compose, add this to your `docker-compose.yml`:

```yaml
version: '3.8'
services:
  comic-utils:
    # ... other configuration ...
    environment:
      - PUID=1000    # Replace with your actual user ID
      - PGID=1000    # Replace with your actual group ID
      - UMASK=000
      - FLASK_ENV=development
      - MONITOR=no
    volumes:
      - 'F:/Comics:/data'
      - 'F:/downloads:/downloads'
      - 'config-volume:/config'
```

### Step 5: Verify the Fix

After updating PUID/PGID and redeploying, check the container logs. You should see:

```
Mounted volume ownership (for PUID/PGID configuration):
  /data owned by: 1000:1000
  /downloads owned by: 1000:1000
Starting as UID:GID 1000:1000 (umask 000)
```

**No warnings should appear** if the ownership matches.

## Common Issues

### Issue: Still getting permission errors
**Solution**: Make sure you're using the correct user ID from `id -u $USER`

### Issue: Container won't start
**Solution**: Check that the PUID/PGID values are valid numbers (usually 1000+ for regular users)

### Issue: Files created by container have wrong ownership
**Solution**: This is expected on Windows - the container can't change Windows file ownership

## Why This Happens

- **Windows NTFS** doesn't support Unix-style user/group ownership
- **WSL** translates Windows permissions to Unix permissions
- **Docker containers** run as specific Unix users (PUID/PGID)
- **Mismatched IDs** cause permission denied errors when writing to mounted volumes

## Testing

After fixing PUID/PGID, test the download function. It should work without permission errors.

## Alternative Solutions

If you continue having issues, you can:

1. **Run as root** (not recommended for production):
   ```yaml
   environment:
     - PUID=0
     - PGID=0
   ```

2. **Use the RUN_AS_ROOT workaround** (for testing only):
   ```yaml
   environment:
     - RUN_AS_ROOT=true
     - PUID=1000
     - PGID=1000
   ```
   This will run the container as root but still show the correct PUID/PGID in logs.

3. **Use WSL2 with proper integration**:
   - Ensure WSL2 is properly configured
   - Check Windows drive permissions
   - Verify volume mounts are working

## Advanced Troubleshooting

### Check Container Logs
After deploying, check the container logs for detailed permission information:
- Directory ownership
- Write permission tests
- Windows/WSL specific debugging
- Mount information
- Filesystem type information

### Common Windows Permission Issues
1. **Windows ACLs not translating**: Windows permissions don't always map correctly to Unix
2. **WSL2 integration problems**: Drive mounting issues between Windows and WSL
3. **Volume mount permissions**: Docker volume mount permission translation issues

### Testing Permissions
The enhanced entrypoint script now tests:
- Basic directory write permissions
- App-specific file creation patterns
- Mount information
- Filesystem types

### Check Windows File Permissions
1. **Right-click on F:\downloads** → Properties → Security
2. **Ensure your user has "Full control"** or at least "Modify" permissions
3. **Check if there are any "Deny" entries** that might override "Allow" permissions
4. **Verify the "Users" group** has appropriate permissions

### WSL2 Permission Commands
In WSL, you can check permissions with:
```bash
# Check current user
id -u $USER
id -g $USER

# Check directory permissions
ls -la /mnt/f/downloads/

# Test write access
touch /mnt/f/downloads/test_file
rm /mnt/f/downloads/test_file
```

## Support

If you still have issues after following this guide:
1. Check the container logs for ownership warnings
2. Verify your PUID/PGID values match `id -u $USER` and `id -g $USER`
3. Ensure your Windows directories have proper permissions
