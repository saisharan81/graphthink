#!/usr/bin/env python3
"""GraphThink CLI — one-command setup and serve."""

import sys
import os
import subprocess
import shutil

def main():
    if len(sys.argv) < 2:
        print("Usage: graphthink <command>")
        print("")
        print("Commands:")
        print("  serve     Start GraphThink server (requires Docker + Memgraph)")
        print("  status    Check if server is running")
        print("  quickstart  Run the demo script")
        return

    cmd = sys.argv[1]
    
    if cmd == "serve":
        serve()
    elif cmd == "status":
        status()
    elif cmd == "quickstart":
        quickstart()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)

def check_docker():
    """Check if Docker is installed and running."""
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=5)
        return True
    except:
        return False

def check_memgraph():
    """Check if Memgraph container is running."""
    try:
        r = subprocess.run(
            ["docker", "ps", "--filter", "name=memgraph", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5
        )
        return "healthy" in r.stdout or "Up" in r.stdout
    except:
        return False

def check_gateway():
    """Check if the ARCHON Gateway is running."""
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:18788/health")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except:
        return False

def serve():
    """Start Memgraph + Gateway."""
    print("🔮 GraphThink Server Setup")
    print("=" * 40)
    
    # Check Docker
    if not check_docker():
        print("❌ Docker is not running.")
        print("   Install Docker: https://docs.docker.com/get-docker/")
        sys.exit(1)
    print("✅ Docker is running")
    
    # Start Memgraph if not running
    if check_memgraph():
        print("✅ Memgraph is already running")
    else:
        print("📦 Starting Memgraph...")
        r = subprocess.run(
            ["docker", "run", "-d", "--name", "memgraph",
             "-p", "7687:7687", "memgraph/memgraph-platform"],
            capture_output=True, text=True, timeout=30
        )
        if r.returncode == 0:
            print("✅ Memgraph started")
        elif "already in use" in r.stderr:
            print("✅ Memgraph container already exists, starting it...")
            subprocess.run(["docker", "start", "memgraph"], capture_output=True)
            print("✅ Memgraph started")
        else:
            print(f"❌ Failed: {r.stderr}")
            sys.exit(1)
    
    # Check Gateway
    if check_gateway():
        print("✅ GraphThink server is running on http://localhost:18788")
        print()
        print("   Try it:")
        print("     from graphthink import GraphThink")
        print("     gt = GraphThink()")
        print("     gt.store('user', 'Hello GraphThink!')")
        return
    
    print()
    print("❌ GraphThink server is not running.")
    print("   The server (GATEWAY.py) needs to be started separately.")
    print("   For now, download it from:")
    print("   https://github.com/saisharan81/graphthink")
    print()
    print("   In the future, 'graphthink serve' will start everything.")

def status():
    """Check all services."""
    print("🔮 GraphThink Status")
    print("=" * 40)
    
    docker = check_docker()
    print(f"{'✅' if docker else '❌'} Docker: {'running' if docker else 'not found'}")
    
    mg = check_memgraph()
    print(f"{'✅' if mg else '❌'} Memgraph: {'running' if mg else 'not running'}")
    
    gw = check_gateway()
    print(f"{'✅' if gw else '❌'} Gateway: {'running on :18788' if gw else 'not running'}")
    
    if docker and mg and gw:
        print()
        print("🎉 Everything is running! Try:")
        print("   python3 -c \"from graphthink import GraphThink; gt = GraphThink(); print(gt.stats())\"")

def quickstart():
    """Run the demo script if available."""
    script = os.path.join(os.path.dirname(__file__), "..", "examples", "quickstart.py")
    if os.path.exists(script):
        subprocess.run(["python3", script])
    else:
        print("Quickstart script not found.")
        print("Try: pip install graphthink")
