cd /home/pi

echo "[INFO] Removing history files"
sudo rm -rvf {/root,/home/pi}/{.bash_history,.viminfo,.lesshst,.ssh/known_hosts}

echo "[INFO] Emptying /storage"
sudo rm -rvf /storage/*

echo "[INFO] Removing logs"
sudo rm -rvf /var/log/*

echo "[INFO] Shutting down in 5 seconds"
sleep 5
sudo shutdown