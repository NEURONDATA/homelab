FROM n8nio/n8n
 
# Install python3
USER root
RUN apk add --update python3 py3-pip

COPY requirements.txt /home/super/n8n/requirements.txt
 
USER node
RUN python3 -m pip install --user --break-system-packages -r /home/super/n8n/requirements.txt
 
# Add the path of the pipx installation to PATH
ENV PATH="/home/node/.local/bin:$PATH"