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


class MemoryMonitor:
    """
    Memory monitoring and management utility.
    """
    
    def __init__(self, threshold_mb=1500, cleanup_threshold_mb=1000):
        """
        Initialize memory monitor.

        Args:
            threshold_mb: Memory threshold in MB to trigger warnings
            cleanup_threshold_mb: Memory threshold in MB to trigger cleanup
        """
        self.threshold_mb = threshold_mb
        self.cleanup_threshold_mb = cleanup_threshold_mb
        self.process = psutil.Process()
        self.monitoring = False
        self.monitor_thread = None
        self._last_cleanup_time = 0
        self._min_cleanup_interval = 300  # Minimum 5 minutes between cleanups
        
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
        
        if memory_mb > self.threshold_mb:
            app_logger.warning(f"High memory usage detected: {memory_mb:.1f}MB")
            
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

            # Only log if we freed meaningful memory or explicitly requested
            if log_always or freed_mb > 1.0 or collected > 100:
                app_logger.info(f"Memory cleanup: freed {freed_mb:.1f}MB, collected {collected} objects")

            self._last_cleanup_time = time.time()
            return freed_mb

        except Exception as e:
            app_logger.error(f"Error during memory cleanup: {e}")
            return 0
    
    def should_cleanup(self):
        """
        Check if cleanup is needed based on memory usage.
        """
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

                    if memory_mb > self.threshold_mb:
                        app_logger.warning(f"High memory usage: {memory_mb:.1f}MB (threshold: {self.threshold_mb}MB)")

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