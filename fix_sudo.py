import re
with open('vectis/roles/dns_role.py', 'r') as f:
    content = f.read()
new_content = re.sub('(with ui\\.spinner\\("Restarting dnsmasq.*?:\\n)', 'subprocess.run(["sudo", "-v"], check=True)\\n        \\1', content, flags=re.DOTALL)
with open('vectis/roles/dns_role.py', 'w') as f:
    f.write(new_content)
