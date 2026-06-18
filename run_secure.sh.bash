#                             Online Bash Shell.
#                 Code, Compile, Run and Debug Bash script online.
# Write your code in this editor and press "Run" button to execute it.


#!/bin/bash

# --------------------------------------------------
# Script de lancement sécurisé de l'app Streamlit
# --------------------------------------------------

PORT=8501
ADDRESS="127.0.0.1"

echo "🔐 Lancement sécurisé de l'application sur http://$ADDRESS:$PORT"

# Forcer l'exécution uniquement en localhost
export STREAMLIT_SERVER_HEADLESS=true
export STREAMLIT_SERVER_PORT=$PORT
export STREAMLIT_BROWSER_SERVER_ADDRESS=$ADDRESS
export STREAMLIT_SERVER_ENABLECORS=false
export STREAMLIT_SERVER_ENABLEXSENDHEADERS=false
export STREAMLIT_LOG_LEVEL=ERROR

# Lancer l'application Streamlit
streamlit run app.py --server.address=$ADDRESS --server.port=$PORT
