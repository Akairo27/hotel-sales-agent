#!/bin/sh
# يُنسخ إلى /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh (0755،
# root:root)، وينفَّذه certbot تلقائياً بعد كل تجديد ناجح. إجباري هنا:
# إضافة nginx لا تُركِّب شهادات IP، فلا تعيد تحميل nginx من تلقاء نفسها.
systemctl reload nginx
