import sys
import os
import yaml
import logging

# Add flood_system and bitchat-python to path using absolute reference from this file
current_dir = os.path.dirname(os.path.abspath(__file__))
root_dir = os.path.dirname(current_dir)
bitchat_path = os.path.join(root_dir, 'bitchat-python')

if bitchat_path not in sys.path:
    sys.path.append(bitchat_path)

from alerts import AlertManager

# Setup logging
logging.basicConfig(level=logging.DEBUG)

def test_dispatch():
    # Load config
    with open('config.yaml', 'r') as f:
        config = yaml.safe_load(f)
    
    # Force cooldowns to 0 for testing
    config['alerts']['cooldown']['sms'] = 0
    config['alerts']['cooldown']['nostr'] = 0
    config['alerts']['cooldown']['ble'] = 0
    config['bitchat']['cooldown'] = 0
    
    # Initialize AlertManager
    print("Initializing AlertManager...")
    manager = AlertManager(config)
    
    # Wait a bit for background initialization (Bitchat thread etc)
    import time
    time.sleep(5)
    
    print("\n--- Triggering Test Alert (WARNING) ---")
    # Trigger an alert by evaluating a water level
    # Warning threshold is 150 in config.yaml
    level = manager.evaluate(160)
    print(f"Current Level: {level}")
    
    print("\n--- Triggering Test Alert (DANGER) ---")
    # Danger threshold is 260
    level = manager.evaluate(270)
    print(f"Current Level: {level}")
    
    # Wait for all async threads to finish
    print("\nWaiting for dispatches to complete...")
    time.sleep(20)
    
    print("\nTest completed. Check logs for success/failure messages.")
    manager.shutdown()

if __name__ == "__main__":
    test_dispatch()
