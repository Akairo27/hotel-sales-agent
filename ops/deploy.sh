#!/bin/bash
# يُشغَّل بصلاحية الجذر على خادم العرض (149.104.71.71). git pull والبناء
# ينفَّذان بمستخدم www-data عبر su -s — مطابقةً لِما اختُبر فعلياً أثناء
# النشر (لا sudo: المستودع مملوك لـ www-data، وتشغيل git بالجذر عليه
# يفتح مشكلة "dubious ownership" التي لا داعي لها). إعادة تشغيل systemd
# وحدها تتطلب الجذر فعلاً.
set -euo pipefail

REPO_DIR=/srv/hotel-admin/app
ADMIN_DIR=/srv/hotel-admin/app/admin
BUILD_ENV=/etc/hotel-admin/build.env
NPM_CACHE=/var/cache/hotel-admin-npm

su -s /bin/bash www-data -c "
  set -e
  cd '$REPO_DIR'
  git pull
  set -a; . '$BUILD_ENV'; set +a
  cd '$ADMIN_DIR'
  npm ci --ignore-scripts --cache='$NPM_CACHE'
  npm run build
"

systemctl restart hotel-admin
systemctl is-active --quiet hotel-admin
echo "hotel-admin restarted and active"
