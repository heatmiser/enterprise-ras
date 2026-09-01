from airlib.env import load_air_config
from airlib.auth import authenticate
from airlib.api import _headers, _api
import httpx, json

cfg = load_air_config(arch="", site="")
base = cfg["base_url"]
api_url = _api(base)
with httpx.Client(verify=False) as client:
    token = authenticate(client, base, cfg.get("username",""), cfg["api_key"])
    headers = _headers(token)
    sim_id = "71e4de61-8d9d-4941-840c-730e8e375d71"
    url = f"{api_url}/api/v3/simulations/nodes/?simulation={sim_id}&limit=50"
    resp = client.get(url, headers=headers)
    for n in resp.json().get("results", []):
        if n.get("name") == "ipp5-285-rh-gpu-01":
            print(f"Node: {n.get('name')}")
            print(json.dumps(n.get("cloud_init"), indent=2))
