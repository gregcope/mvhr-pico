import machine
import os
import time
import logging

# Setup standard logging for the console
logging.basicConfig(level=logging.INFO)
logger: logging.Logger = logging.getLogger(__name__)

flag_file: str = "ota_pending.flag"


def write_crash_log(error_name: str, error_detail: str) -> None:
    """
    Write the error details to a local log file.
    Using 'w' mode overwrites the file, keeping only the last error.
    """
    try:
        with open("boot_error.log", "w") as log_file:
            log_file.write(f"Boot failed due to {error_name}: {error_detail}\n")
    except OSError as file_err:
        # Evaluate specific OS network/storage error codes
        logger.error("Failed to write crash log. Errno: %s", file_err.args[0])


def trigger_rollback() -> None:
    """Attempt to restore the backup main.py and reset the device."""
    logger.info("Attempting to restore from main_backup.py")
    try:
        # Remove the corrupted file, explicitly ignoring ENOENT (file not found)
        try:
            os.remove("main.py")
        except OSError as remove_err:
            if remove_err.args[0] != 2:
                raise remove_err

        # Restore the known-good backup
        os.rename("main_backup.py", "main.py")
        
        # Clear the flag so the restored version boots normally
        try:
            os.remove(flag_file)
        except OSError:
            pass
            
        logger.info("Rollback successful. Rebooting.")
        machine.reset()

    except OSError as rollback_error:
        logger.error("Rollback failed. Errno: %s", rollback_error.args[0])
        write_crash_log("RollbackError", f"Errno {rollback_error.args[0]}")
        
        # Sleep for five minutes before rebooting to prevent rapid flash degradation
        time.sleep(300)
        machine.reset()


def is_ota_pending() -> bool:
    """Check if an OTA update is awaiting validation."""
    try:
        os.stat(flag_file)
        return True
    except OSError as e:
        # Code 2 is ENOENT (No such file or directory)
        if e.args[0] == 2:
            return False
        return False


# Give the hardware a moment to settle
time.sleep(1)

# Determine our boot state before importing the main application
ota_is_pending: bool = is_ota_pending()

try:
    # Explicitly importing main allows us to catch syntax or initialisation errors
    import main
    
except SyntaxError as e:
    logger.error("SyntaxError in main.py: %s", e)
    write_crash_log("SyntaxError", str(e))
    
    if ota_is_pending:
        trigger_rollback()
    else:
        time.sleep(60)
        machine.reset()
        
except ImportError as e:
    logger.error("ImportError in main.py: %s", e)
    write_crash_log("ImportError", str(e))
    
    if ota_is_pending:
        trigger_rollback()
    else:
        time.sleep(60)
        machine.reset()
        
except Exception as e:
    # Catching generic Exception ensures the device never hangs while inaccessible
    error_type: str = type(e).__name__
    logger.error("Critical error in main.py (%s): %s", error_type, e)
    write_crash_log(error_type, str(e))
    
    if ota_is_pending:
        logger.warning("Crash occurred during OTA validation. Rolling back.")
        trigger_rollback()
    else:
        logger.warning("Transient runtime crash detected. Rebooting normally in one minute.")
        time.sleep(60)
        machine.reset()
