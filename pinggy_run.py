import pinggy

tunnel = pinggy.start_tunnel(forwardto="localhost:8501")
print("Public URL:", tunnel.urls[0])