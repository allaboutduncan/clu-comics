"""
Memory management utilities for comic-utils application.
Provides tools for monitoring memory usage, cleanup, and optimization.
"""

import gc
import psutil
import os
import sys
import time
import threading
from contextlib import contextmanager
from core.app_logging import app_logger
import tracemalloc


def _threshold_from_env(name, default):
    """Read a positive MB threshold from the environment, else ``default``."""
    try:
        value = int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default
    return value if value > 0 else default


class MemoryMonitor:
    """
    Memory monitoring and management utility.
    """

    def __init__(self, threshold_mb=None, cleanup_threshold_mb=None):
        """
        Initialize memory monitor.

        Args:
            threshold_mb: Memory threshold in MB to trigger warnings. Defaults to
                MEMORY_THRESHOLD_MB, or 1500.
            cleanup_threshold_mb: Memory threshold in MB to trigger cleanup.
                Defaults to MEMORY_CLEANUP_THRESHOLD_MB, or 1000.

        The thresholds are configurable because the warning re-fires every poll
        (60s) for as long as RSS stays above it, so a deployment whose normal
        working set is legitimately larger than the default would otherwise fill
        its logs with a warning it can do nothing about.
        """
        self.threshold_mb = (
            _threshold_from_env("MEMORY_THRESHOLD_MB", 1500)
            if threshold_mb is None
            else threshold_mb
        )
        self.cleanup_threshold_mb = (
            _threshold_from_env("MEMORY_CLEANUP_THRESHOLD_MB", 1000)
            if cleanup_threshold_mb is None
            else cleanup_threshold_mb
        )
        self.process = psutil.Process()
        self.monitoring = False
        self.monitor_thread = None
        self._last_cleanup_time = 0
        self._min_cleanup_interval = 300  # Minimum 5 minutes between cleanups

        # State behind the two log-noise rules below. The warning used to fire
        # on every 60s poll for as long as RSS stayed high: 215 lines of one
        # reported log, all identical, saying nothing the first one had not.
        self._high_since = None          # when RSS first crossed the threshold
        self._last_high_warning = 0      # when we last said so
        self._ineffective_cleanups = 0   # consecutive gc.collect()s that freed ~0
        self._gave_up_warned = False
        
    def get_memory_usage(self):
        """
        Get current memory usage in MB.
        """
        try:
            memory_info = self.process.memory_info()
            return memory_info.rss / 1024 / 1024  # Convert to MB
        except Exception as e:
            app_logger.error(f"Error getting memory usage: {e}")
            return 0
    
    def get_memory_percent(self):
        """
        Get memory usage as percentage of system memory.
        """
        try:
            return self.process.memory_percent()
        except Exception as e:
            app_logger.error(f"Error getting memory percentage: {e}")
            return 0
    
    def log_memory_usage(self, context=""):
        """
        Log current memory usage.
        """
        memory_mb = self.get_memory_usage()
        # memory_percent = self.get_memory_percent()
        # app_logger.info(f"Memory usage {context}: {memory_mb:.1f}MB ({memory_percent:.1f}%)")
        
        # Routed through the same reporter as the poll loop. memory_context()
        # calls this twice per guarded operation, so an unthrottled warning here
        # is a second source of the same noise.
        self._report_high_memory(memory_mb)

        return memory_mb
    
    def force_cleanup(self, log_always=False):
        """
        Force garbage collection and memory cleanup.

        Args:
            log_always: If True, log even when nothing was freed
        """
        try:
            # Get memory before cleanup
            memory_before = self.get_memory_usage()

            # Force garbage collection
            collected = gc.collect()

            # Additional cleanup steps
            if hasattr(gc, 'collect_generations'):
                gc.collect_generations()

            memory_after = self.get_memory_usage()
            freed_mb = memory_before - memory_after

            # Only log if we freed meaningful memory or explicitly requested.
            # `collected > 100` is deliberately NOT a reason on its own any
            # more: a run that collects tens of thousands of objects and frees
            # nothing is the uninformative line, not the informative one.
            if log_always or freed_mb > 1.0:
                app_logger.info(f"Memory cleanup: freed {freed_mb:.1f}MB, collected {collected} objects")
            else:
                app_logger.debug(f"Memory cleanup: freed {freed_mb:.1f}MB, collected {collected} objects")

            if freed_mb > 1.0:
                self._ineffective_cleanups = 0
                self._gave_up_warned = False
            else:
                self._ineffective_cleanups += 1
                if (self._ineffective_cleanups >= self.INEFFECTIVE_CLEANUP_LIMIT
                        and not self._gave_up_warned):
                    self._gave_up_warned = True
                    app_logger.warning(
                        f"{self._ineffective_cleanups} consecutive garbage "
                        f"collections freed nothing, so the memory in use is "
                        f"not collectable garbage. Pausing automatic cleanup; "
                        f"it resumes if usage drops below "
                        f"{self.threshold_mb}MB."
                    )

            self._last_cleanup_time = time.time()
            return freed_mb

        except Exception as e:
            app_logger.error(f"Error during memory cleanup: {e}")
            return 0
    
    # How long to wait before repeating the high-memory warning. The first
    # crossing is news; the eightieth identical line an hour later is not, and
    # the operator cannot act on information they have already been given.
    HIGH_MEMORY_REPEAT_SECONDS = 30 * 60

    # Consecutive cleanups that free nothing before we stop claiming to be
    # cleaning up. gc.collect() only reclaims reference cycles -- it cannot
    # return freed heap to the OS, nor touch memory something still holds -- so
    # "freed 0.0MB, collected 63817 objects", over and over, means the growth is
    # not cyclic garbage and this tool is not the one that will fix it.
    INEFFECTIVE_CLEANUP_LIMIT = 5

    def _report_high_memory(self, memory_mb):
        """Warn on crossing the threshold, then only occasionally."""
        if memory_mb <= self.threshold_mb:
            if self._high_since is not None:
                app_logger.info(
                    f"Memory back below the threshold: {memory_mb:.1f}MB"
                )
            self._high_since = None
            self._last_high_warning = 0
            self._ineffective_cleanups = 0
            self._gave_up_warned = False
            return

        now = time.time()
        if self._high_since is None:
            self._high_since = now
            self._last_high_warning = now
            app_logger.warning(
                f"High memory usage: {memory_mb:.1f}MB "
                f"(threshold: {self.threshold_mb}MB)"
            )
            return

        if now - self._last_high_warning >= self.HIGH_MEMORY_REPEAT_SECONDS:
            self._last_high_warning = now
            app_logger.warning(
                f"Memory still high: {memory_mb:.1f}MB "
                f"(threshold: {self.threshold_mb}MB, "
                f"for {(now - self._high_since) / 60:.0f} minutes)"
            )
        else:
            app_logger.debug(f"Memory usage: {memory_mb:.1f}MB")

    def should_cleanup(self):
        """
        Check if cleanup is needed based on memory usage.
        """
        if self._ineffective_cleanups >= self.INEFFECTIVE_CLEANUP_LIMIT:
            # Still poll and still report; just stop running a collection that
            # has demonstrably nothing to collect.
            return False
        memory_mb = self.get_memory_usage()
        return memory_mb > self.cleanup_threshold_mb
    
    def start_monitoring(self, interval=60):
        """
        Start background memory monitoring.

        Args:
            interval: Monitoring interval in seconds (default: 60)
        """
        if self.monitoring:
            return

        self.monitoring = True

        def monitor_loop():
            while self.monitoring:
                try:
                    memory_mb = self.get_memory_usage()

                    self._report_high_memory(memory_mb)

                    # Only cleanup if above threshold AND enough time has passed
                    if self.should_cleanup():
                        time_since_last = time.time() - self._last_cleanup_time
                        if time_since_last >= self._min_cleanup_interval:
                            app_logger.debug(f"Background memory cleanup triggered (usage: {memory_mb:.1f}MB)")
                            self.force_cleanup()

                except Exception as e:
                    app_logger.error(f"Error in memory monitoring: {e}")

                time.sleep(interval)

        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()
        app_logger.info(f"Memory monitoring started (interval: {interval}s, cleanup threshold: {self.cleanup_threshold_mb}MB)")
    
    def stop_monitoring(self):
        """
        Stop background memory monitoring.
        """
        self.monitoring = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        app_logger.info("Memory monitoring stopped")


@contextmanager
def memory_context(operation_name="", cleanup_threshold_mb=500):
    """
    Context manager for memory-aware operations.
    
    Args:
        operation_name: Name of the operation for logging
        cleanup_threshold_mb: Memory threshold to trigger cleanup
    """
    monitor = MemoryMonitor(cleanup_threshold_mb=cleanup_threshold_mb)
    
    try:
        memory_before = monitor.log_memory_usage(f"before {operation_name}")
        yield monitor
    finally:
        memory_after = monitor.log_memory_usage(f"after {operation_name}")
        memory_diff = memory_after - memory_before
        
        if memory_diff > 100:  # More than 100MB increase
            app_logger.warning(f"Significant memory increase during {operation_name}: +{memory_diff:.1f}MB")
            monitor.force_cleanup()
        elif memory_diff < -50:  # More than 50MB decrease
            app_logger.info(f"Memory freed during {operation_name}: {memory_diff:.1f}MB")


def optimize_for_large_files(file_size_mb):
    """
    Optimize memory settings based on file size.
    
    Args:
        file_size_mb: Size of file being processed in MB
    """
    if file_size_mb > 1000:  # 1GB+
        # Increase PIL's image pixel limit for very large files
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 1000000000  # 1 billion pixels
        
        # Force garbage collection before processing
        gc.collect()
        
        app_logger.info(f"Optimized memory settings for large file ({file_size_mb:.1f}MB)")
    
    elif file_size_mb > 100:  # 100MB+
        # Moderate optimization
        gc.collect()
        app_logger.info(f"Applied moderate memory optimization for file ({file_size_mb:.1f}MB)")


def check_system_memory():
    """
    Check system memory availability and log warnings if low.
    """
    try:
        memory = psutil.virtual_memory()
        available_gb = memory.available / 1024 / 1024 / 1024
        
        app_logger.info(f"System memory: {available_gb:.1f}GB available, {memory.percent}% used")
        
        if available_gb < 1.0:  # Less than 1GB available
            app_logger.warning(f"Low system memory: {available_gb:.1f}GB available")
            return False
        elif available_gb < 2.0:  # Less than 2GB available
            app_logger.warning(f"Moderate system memory: {available_gb:.1f}GB available")
            return True
        else:
            return True
            
    except Exception as e:
        app_logger.error(f"Error checking system memory: {e}")
        return True


def setup_memory_tracing():
    """
    Setup memory tracing for debugging memory leaks.
    """
    try:
        tracemalloc.start()
        app_logger.info("Memory tracing enabled")
    except Exception as e:
        app_logger.error(f"Failed to enable memory tracing: {e}")


def get_memory_snapshot():
    """
    Get current memory snapshot for debugging.
    """
    try:
        if tracemalloc.is_tracing():
            snapshot = tracemalloc.take_snapshot()
            top_stats = snapshot.statistics('lineno')
            
            app_logger.info("Top 10 memory allocations:")
            for stat in top_stats[:10]:
                app_logger.info(f"  {stat.count} blocks: {stat.size / 1024:.1f} KB")
                app_logger.info(f"    {stat.traceback.format()}")
                
            return snapshot
    except Exception as e:
        app_logger.error(f"Error taking memory snapshot: {e}")
        return None


def cleanup_temp_files(temp_dir=None):
    """
    Clean up temporary files to free disk space.
    
    Args:
        temp_dir: Directory to clean (defaults to system temp)
    """
    if temp_dir is None:
        temp_dir = os.path.join(os.getcwd(), "temp")
    
    try:
        if not os.path.exists(temp_dir):
            return
            
        cleaned_size = 0
        cleaned_count = 0
        
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                file_path = os.path.join(root, file)
                try:
                    file_size = os.path.getsize(file_path)
                    os.remove(file_path)
                    cleaned_size += file_size
                    cleaned_count += 1
                except Exception as e:
                    app_logger.warning(f"Failed to remove temp file {file_path}: {e}")
        
        if cleaned_count > 0:
            cleaned_mb = cleaned_size / 1024 / 1024
            app_logger.info(f"Cleaned up {cleaned_count} temp files, freed {cleaned_mb:.1f}MB")
            
    except Exception as e:
        app_logger.error(f"Error cleaning temp files: {e}")


# Global memory monitor instance
global_monitor = MemoryMonitor()


def get_global_monitor():
    """
    Get the global memory monitor instance.
    """
    return global_monitor


def initialize_memory_management():
    """
    Initialize memory management for the application.
    """
    try:
        # Check system memory
        if not check_system_memory():
            app_logger.warning("System memory is low, performance may be affected")
        
        # Start background monitoring
        global_monitor.start_monitoring()
        
        # Setup memory tracing in debug mode
        if os.environ.get('DEBUG', '').lower() in ('true', '1', 'yes'):
            setup_memory_tracing()
        
        app_logger.info("Memory management initialized")
        
    except Exception as e:
        app_logger.error(f"Error initializing memory management: {e}")


def cleanup_on_exit():
    """
    Cleanup function to call on application exit.
    """
    try:
        global_monitor.stop_monitoring()
        global_monitor.force_cleanup()
        cleanup_temp_files()
        
        if tracemalloc.is_tracing():
            tracemalloc.stop()
            
        app_logger.info("Memory management cleanup completed")
        
    except Exception as e:
        app_logger.error(f"Error during memory cleanup on exit: {e}") 