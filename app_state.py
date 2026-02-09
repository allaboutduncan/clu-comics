import threading
from apscheduler.schedulers.background import BackgroundScheduler

# ── Schedulers ──
rebuild_scheduler = BackgroundScheduler(daemon=True)
sync_scheduler = BackgroundScheduler(daemon=True)
getcomics_scheduler = BackgroundScheduler(daemon=True)
weekly_packs_scheduler = BackgroundScheduler(daemon=True)

# ── Wanted Issues Refresh ──
wanted_refresh_in_progress = False
wanted_refresh_lock = threading.Lock()
wanted_last_refresh_time = 0  # timestamp of last completed refresh

# ── Data Directory Stats Cache ──
data_dir_stats_cache = {}
data_dir_stats_last_update = 0
DATA_DIR_STATS_CACHE_DURATION = 300  # 5 minutes
