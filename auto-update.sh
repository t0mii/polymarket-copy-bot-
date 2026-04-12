#!/bin/bash
cd /root/polymarket-copy-bot
LOG=/root/polymarket-copy-bot/logs/auto-update.log

# Fetch upstream (developer)
git fetch upstream 2>/dev/null || exit 0

# Check if upstream/main has new commits vs our branch
NEW_COMMITS=$(git log HEAD..upstream/main --oneline 2>/dev/null)
if [ -z "$NEW_COMMITS" ]; then
    exit 0
fi

# Save current commit for rollback
OLD_COMMIT=$(git rev-parse HEAD)

# Log what is new
echo "" >> $LOG
echo "========================================" >> $LOG
echo "$(date '+%Y-%m-%d %H:%M:%S') - NEUE UPDATES VOM ENTWICKLER" >> $LOG
echo "========================================" >> $LOG
echo "$NEW_COMMITS" >> $LOG
echo "" >> $LOG
echo "Details:" >> $LOG
git log HEAD..upstream/main --format='%h %s (%an, %ad)' --date=short >> $LOG
echo "" >> $LOG
echo "Geaenderte Dateien:" >> $LOG
git diff --stat HEAD..upstream/main >> $LOG
echo "----------------------------------------" >> $LOG

# Merge - bei Konflikten unsere Aenderungen behalten
git merge upstream/main --no-edit -X ours 2>&1 >> $LOG

# Syntax-Check: alle Python-Dateien pruefen die sich geaendert haben
SYNTAX_OK=true
for f in $(git diff --name-only $OLD_COMMIT HEAD -- '*.py'); do
    if [ -f "$f" ]; then
        if ! /root/polymarket-copy-bot/venv/bin/python -m py_compile "$f" 2>> $LOG; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') - SYNTAX ERROR in $f" >> $LOG
            SYNTAX_OK=false
        fi
    fi
done

if [ "$SYNTAX_OK" = false ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - ROLLBACK: Syntax-Fehler gefunden, zurueck zu $OLD_COMMIT" >> $LOG
    git reset --hard $OLD_COMMIT 2>&1 >> $LOG
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Rollback abgeschlossen, Service laeuft weiter auf alter Version" >> $LOG
    exit 1
fi

# Push zu GitLab
git push origin piff-custom 2>&1 >> $LOG

# Restart service
systemctl restart copybot

# Health-Check: 30 Sekunden warten, dann pruefen ob Service noch laeuft
sleep 30

if systemctl is-active --quiet copybot; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') - Auto-update ERFOLGREICH, Service laeuft" >> $LOG
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') - SERVICE CRASHED nach Update! Starte Rollback..." >> $LOG
    
    # Rollback auf alten Commit
    git reset --hard $OLD_COMMIT 2>&1 >> $LOG
    git push origin piff-custom --force 2>&1 >> $LOG
    
    # Restart mit alter Version
    systemctl restart copybot
    sleep 10
    
    if systemctl is-active --quiet copybot; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') - ROLLBACK ERFOLGREICH, Service laeuft wieder auf alter Version" >> $LOG
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') - KRITISCH: Auch nach Rollback crashed! Manuell pruefen!" >> $LOG
    fi
    exit 1
fi
