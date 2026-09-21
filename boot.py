import machine
import os
import time

flag_file: str = "ota_pending.flag"
main_file: str = "main.py"


def write_crash_log(error_name: str, error_detail: str) -> None:
    """Write error details to a local log file, overwriting previous entries."""
    try:
        with open("boot_error.log", "w") as log_file:
            log_file.write(f"Boot failed due to {error_name}: {error_detail}\n")
    except OSError:
        pass


def trigger_rollback() -> None:
    """Attempt to restore the backup main.py and reset the device."""
    try:
        try:
            os.remove(main_file)
        except OSError as remove_err:
            if remove_err.args[0] != 2:
                raise remove_err

        os.rename("main_backup.py", main_file)
        
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


def main_exists() -> bool:
    """Check if main.py is present on the filesystem."""
    try:
        os.stat(main_file)
        return True
    except OSError:
        return False


time.sleep(1)

# If main.py does not exist yet (initial bootstrap), exit boot.py 
# cleanly to allow REPL access for ugit.pull_all()
if not main_exists():
    raise SystemExit

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
