import machine
import os
import time

flag_file: str = "ota_pending.flag"


def write_crash_log(error_name: str, error_detail: str) -> None:
    """
    Write the error details to a local log file.
    Using 'w' mode overwrites the file, keeping only the last error.
    """
    try:
        with open("boot_error.log", "w") as log_file:
            log_file.write(f"Boot failed due to {error_name}: {error_detail}\n")
    except OSError:
        pass


def trigger_rollback() -> None:
    """Attempt to restore the backup main.py and reset the device."""
    try:
        try:
            os.remove("main.py")
        except OSError as remove_err:
            if remove_err.args[0] != 2:
                raise remove_err

        os.rename("main_backup.py", "main.py")
        
        try:
            os.remove(flag_file)
        except OSError:
            pass
            
        machine.reset()

    except OSError as rollback_error:
        write_crash_log("RollbackError", f"Errno {rollback_error.args[0]}")
        time.sleep(300)
        machine.reset()


def is_ota_pending() -> bool:
    """Check if an OTA update is awaiting validation."""
    try:
        os.stat(flag_file)
        return True
    except OSError as e:
        if e.args[0] == 2:
            return False
        return False


time.sleep(1)

ota_is_pending: bool = is_ota_pending()

try:
    import main
    
except SyntaxError as e:
    write_crash_log("SyntaxError", str(e))
    
    if ota_is_pending:
        trigger_rollback()
    else:
        time.sleep(60)
        machine.reset()
        
except ImportError as e:
    write_crash_log("ImportError", str(e))
    
    if ota_is_pending:
        trigger_rollback()
    else:
        time.sleep(60)
        machine.reset()
        
except Exception as e:
    error_type: str = type(e).__name__
    write_crash_log(error_type, str(e))
    
    if ota_is_pending:
        trigger_rollback()
    else:
        time.sleep(60)
        machine.reset()
