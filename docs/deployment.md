# دليل نشر لوحة الإدارة — خادم العرض

هذا الخادم (`149.104.71.71`) **عرض توضيحي للعميل، لا نشر إنتاجي.**
يستضيف `admin/` فقط — خدمات FastAPI لا تدخل هذا النشر، لأن كل إجراءات
اللوحة تنادي Supabase مباشرة عبر RPC، بلا أي `fetch(` أو `localhost`
داخل `admin/app`. قاعدة البيانات هي `hotel-sales-agent-dev` نفسها، بلا
تغيير.

الخطة المعتمدة الكاملة (بما فيها الأسباب والقيود السبعة التي بُنيت
عليها كل قرار هنا) في محادثة النشر الأصلية؛ هذا الملف هو الخلاصة
التشغيلية الدائمة منها.

---

## المعمار

```
الإنترنت ──443──► nginx ──proxy_pass──► 127.0.0.1:3000
            80 ──┤                       next start (systemd)
                 └──► /var/www/certbot/.well-known/acme-challenge

/srv/hotel-admin/app/                استنساخ المستودع
/etc/hotel-admin/admin.env           الأسرار، root:root 0600، يقرؤه systemd
/etc/hotel-admin/build.env           NEXT_PUBLIC_* فقط، root:root 0644
/etc/systemd/system/hotel-admin.service
/etc/nginx/sites-available/hotel-admin
/etc/nginx/conf.d/hotel-admin-limits.conf
/etc/letsencrypt/live/149.104.71.71/
/var/cache/hotel-admin-npm           مخزن npm، www-data
```

لا `ufw` ولا أي جدار على الجهاز — منافذ لوحة المزوّد (٢٢/٨٠/٤٤٣) هي
كل الحماية على مستوى الشبكة. **الربط على `127.0.0.1:3000` في وحدة
systemd هو الحاجز الوحيد** الذي يمنع كشف Next.js مباشرة على الإنترنت؛
لا طبقة احتياطية خلفه. أي تعديل مستقبلي على `ExecStart` يجب أن يبقي
`--hostname 127.0.0.1` أو يضيف جداراً بديلاً أولاً.

---

## قواعد تشغيل دائمة

هذه ليست ملاحظات جلسة — خُذها كقيود ثابتة على أي عمل مستقبلي على هذا
الخادم:

1. **لا مفتاح حقيقي يدخل سياق أي جلسة عمل ولا المستودع.** الأسرار الثلاثة
   (`NEXT_PUBLIC_SUPABASE_URL`، `NEXT_PUBLIC_SUPABASE_ANON_KEY`،
   `SUPABASE_SERVICE_ROLE_KEY`) تنتقل بنسخ ملف (`scp`) لا بلصق قيمة، وتبقى
   في `/etc/hotel-admin/`، خارج `/srv/hotel-admin/app` تماماً.
2. **أي كتابة أو استبدال خارج `/srv/hotel-admin/app` تحتاج إذناً صريحاً
   قبل التنفيذ** — إنشاء ملفات جديدة في مسارات نظام معروفة (نظير
   `/etc/nginx/sites-available/`) مستثنى، أما الحذف أو الاستبدال (حذف
   موقع nginx الافتراضي، `certbot delete`، `apt upgrade`) فلا يُنفَّذ
   بلا سؤال أولاً.
3. **`next build` إن انكسر: يُبلَّغ الخطأ حرفياً ويُوقَف، لا يُعدَّل
   الكود ولا تُخفَّف رايات `--ignore-scripts`.**
4. **كل تحقق يكون بأمر يقرأ الحالة فعلياً بعد كل تغيير** — لا افتراض من
   قراءة ملف إعداد أن التغيير أُعمِل. مثال حي: تحقّق `Restart=always`
   الفعلي كان بقتل العملية حقاً (`systemctl kill -s SIGKILL`) ومراقبة
   السجل، لا بالاكتفاء بقراءة الوحدة.
5. **هوية تشغيل الخدمة `www-data`** — موجودة افتراضياً في أوبنتو، فلا
   إنشاء مستخدم جديد يخالف قيد "لا تعديل SSH ولا مستخدمين". أي عملية
   تلمس `/srv/hotel-admin/app` (بناء، `git pull`) تُنفَّذ بهذا المستخدم
   عبر `su -s /bin/bash www-data -c '...'`، **لا `sudo`** — الأخير محظور
   حرفياً في `.claude/settings.json` لهذا المستودع.

---

## المسار الصحيح لـ SSH/SCP على ويندوز

استخدم عميل ويندوز الأصلي **`C:\Windows\System32\OpenSSH\ssh.exe`**
(والمكافئ لـ `scp.exe`) — **وليس** `ssh`/`scp` الافتراضيين على `PATH`
داخل Git Bash (`/usr/bin/ssh`، من توزيعة Git for Windows).

**السبب:** المفتاح الخاص محميّ بكلمة مرور ومُحمَّل في خدمة ويندوز
`ssh-agent` عبر أنبوب مسمّى نظامي، لا متغير بيئة `SSH_AUTH_SOCK`. عميل
Git Bash لا يعرف كيف يتحدّث مع أنبوب هذه الخدمة، فيفشل بخطأ
`Permission denied (publickey,password)` يوهم بأن المفتاح خاطئ بينما
السبب الحقيقي هو العميل الخطأ. إن ظهر هذا الخطأ رغم أن `ssh-add -l` (من
PowerShell) يُظهر المفتاح محمَّلاً، فالمشكلة العميل المستخدَم لا المفتاح.

---

## إعادة البناء من الصفر — مرحلة بمرحلة

### المرحلة ٠ — قبل لمس الخادم

```bash
cd admin && npm run build -- --webpack   # محلياً على ويندوز فقط
```

`--webpack` إلزامية على جهاز التطوير تحديداً (حظر مكتبات يُسقط Turbopack
بخطأ ثابت) — **الخادم لا يحتاجها**، أوبنتو نظيف لا علاقة له بهذا الحظر.

جسّ الخادم قراءةً فقط قبل أي تغيير:
```bash
cat /etc/os-release; free -m; nproc; df -h /
getent passwd | awk -F: '$3>=1000 && $3<65534'
ss -ltnp; systemctl is-active nginx; node -v
```

**ملف التبديل (swap) موجود مسبقاً من مزوّد الاستضافة** — ٤ غيغابايت
(`/swap.img`)، ظهر عند أول فحص، لم تُنشئه أي خطوة في هذا الدليل.
**لا تُنشئ ملف تبديل جديد ولا تفترض غيابه** — أي خطوة تعتمد على وجود
تبديل (لتفادي قتل `next build` صامتاً على ذاكرة محدودة) يكفيها التأكد
من وجود الحالي عبر `free -h`/`swapon --show`.

### المرحلة ١ — nginx وsnapd

```bash
apt update
apt install -y nginx snapd
```

**بلا `apt upgrade`** — ترقية شاملة تستبدل حزماً وقد تكسر خادماً يعمل،
غير لازمة لعرض توضيحي.

**تحقق:** `nginx -v && systemctl is-active nginx && ss -ltnp | grep :80`

### المرحلة ٢ — Node 22

عبر مستودع NodeSource بطريقة الـ keyring، لا `curl | bash`:

```bash
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
  | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
  > /etc/apt/sources.list.d/nodesource.list
apt update && apt install -y nodejs
```

**تحقق:** `node -v` → لا بد `v22.x`.

### المرحلة ٣ — الكود والأسرار

```bash
git clone https://github.com/Akairo27/hotel-sales-agent.git /srv/hotel-admin/app
```

الأسرار تنتقل بنسخ ملف من `admin/.env.local` (لا يُقرأ محتواه ولا
يُطبع في أي جلسة عمل):

```powershell
scp admin\.env.local root@149.104.71.71:/etc/hotel-admin/admin.env
```

ثم يُشتق `build.env` على الخادم بلا طباعة قيمة:
```bash
chmod 0755 /etc/hotel-admin          # ٠٧٥٠ يمنع www-data من عبوره أصلاً
chmod 0600 /etc/hotel-admin/admin.env
grep '^NEXT_PUBLIC_' /etc/hotel-admin/admin.env > /etc/hotel-admin/build.env
chmod 0644 /etc/hotel-admin/build.env
```

السرّ الحقيقي الوحيد هو `SUPABASE_SERVICE_ROLE_KEY`؛
`NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_ANON_KEY` يُرسَلان
حرفياً لكل متصفح أصلاً، فحمايتهما بـ ٠٦٠٠ مسرح أمني لا أمن — والفصل هنا
يُلغي الحاجة للبناء بالجذر.

**تحقق بلا كشف قيمة:**
```bash
stat -c '%a %U:%G' /etc/hotel-admin/admin.env        # 600 root:root
stat -c '%a %U:%G' /etc/hotel-admin/build.env        # 644 root:root
grep -c '^SUPABASE_SERVICE_ROLE_KEY=' /etc/hotel-admin/build.env   # 0
```

### المرحلة ٤ — البناء

```bash
chown -R www-data:www-data /srv/hotel-admin/app
mkdir -p /var/cache/hotel-admin-npm
chown www-data:www-data /var/cache/hotel-admin-npm
su -s /bin/bash www-data -c '
  set -a; . /etc/hotel-admin/build.env; set +a
  cd /srv/hotel-admin/app/admin
  npm ci --ignore-scripts --cache=/var/cache/hotel-admin-npm
  npm run build'
```

**مخزن npm المؤقت (cache) — لماذا `--cache=` إجبارية:** `www-data` في
أوبنتو مُعرَّف بـ `HOME=/var/www`، وهذا جذر nginx القياسي `755 root:root`
— غير قابل للكتابة من `www-data`. و`npm` يخزّن مخزنه المؤقت افتراضياً في
`$HOME/.npm`، فلا يُنشأ أبداً. **لا يفشل بخطأ واضح فوراً** — يفشل
*متقطعاً* أثناء فك الأرشيف (`TAR_ENTRY_ERROR ENOENT`، ثم `ENOTEMPTY` عند
تنظيف npm لنفسه، ملفات مختلفة كل مرة)، يبدو تماماً كسباق تزامن وليس كذلك
(جُرِّب `--maxsockets=1` — لم يُصلح شيئاً). السبب الحقيقي (`EACCES ...
mkdir /var/www/.npm`) لا يظهر إلا أحياناً. **لا تُصلَح بتخفيف صلاحية
`/var/www`** (جذر nginx، أثر خارج نطاق هذا النشر) **ولا بمخزن داخل شجرة
المشروع** (يدخل بالخطأ في نسخ/أرشفة لاحقة). الحل: مخزن مخصص خارج
الاثنين، `/var/cache/hotel-admin-npm`، بملكية `www-data`، ويجب أن يبقى
**ثابتاً هنا وفي `hotel-admin.service` (`npm_config_cache`) وفي
`ops/deploy.sh`** — وإلا تكرر هذا العطل في كل نشر لاحق.

**تحقق:** `.next/BUILD_ID` موجود، `ls .next/static` يعمل، و`git
status`/`git diff --stat` فارغان (لم يُلمَس `package-lock.json`).

### المرحلة ٥ — خدمة systemd

`ops/hotel-admin.service` → `/etc/systemd/system/hotel-admin.service`
(نسخة طبق الأصل، بلا أي قيمة محذوفة — الملف لا يحوي سراً أصلاً، فقط
مسار `EnvironmentFile` الذي يقرؤه مدير systemd بالجذر قبل التحوّل إلى
`User=www-data`؛ هذا ما يحلّ التعارض بين "الملف ٠٦٠٠" و"الخدمة تعمل
بمستخدم غير جذر").

```bash
cp ops/hotel-admin.service /etc/systemd/system/hotel-admin.service
systemctl daemon-reload
systemctl enable hotel-admin
systemctl start hotel-admin
```

**تحقق — الأربعة كلها ضرورية، لا يغني أحدها عن الآخر:**
```bash
systemctl is-enabled hotel-admin; systemctl is-active hotel-admin
ss -ltnp | grep 3000        # يجب 127.0.0.1:3000 لا 0.0.0.0:3000
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3000/login
journalctl -u hotel-admin -n 30 --no-pager
```

**تحقق الانهيار فعلياً، لا بقراءة `Restart=always` فقط:**
```bash
systemctl kill -s SIGKILL hotel-admin
sleep 6
systemctl is-active hotel-admin      # active، وبعملية جديدة (PID مختلف)
```

### المرحلة ٦ — TLS

#### ٦-أ. إثبات وصول المنفذ ٨٠ من الخارج — قبل أي استدعاء لـ certbot

كتلة nginx مؤقتة على المنفذ ٨٠ (`server_name 149.104.71.71`، تخدم
`/var/www/certbot`)، ثم فحص من **خارج الخادم فعلاً** (لا `curl` محلي
عليه، فذاك يثبت أن nginx يعمل لا أن المنفذ يستقبل من الإنترنت):

```powershell
curl.exe -sS -m 10 -w "`n%{http_code}`n" http://149.104.71.71/.well-known/acme-challenge/probe
```

المطلوب: `probe-ok` و`200`. **إن لم يأتِ ٢٠٠ — قف، لا تستدعِ certbot
إطلاقاً.**

#### ٦-ب. تثبيت certbot

```bash
snap install core && snap refresh core
snap install --classic certbot
ln -s /snap/bin/certbot /usr/bin/certbot
certbot --version        # لا بد ≥ 5.4.0 (يعرف --ip-address)
```

#### ٦-ج. الطلب: بيئة اختبار ثم إنتاج، محاولة واحدة لكل بيئة

```bash
certbot certonly --staging --preferred-profile shortlived \
  --webroot --webroot-path /var/www/certbot \
  --ip-address 149.104.71.71 \
  --cert-name staging-149.104.71.71 \
  --agree-tos --non-interactive --register-unsafely-without-email

certbot certonly --preferred-profile shortlived \
  --webroot --webroot-path /var/www/certbot \
  --ip-address 149.104.71.71 \
  --cert-name 149.104.71.71 \
  --agree-tos --non-interactive --register-unsafely-without-email
```

> ⚠️ **`--cert-name` صريح إجباري على طلب الإنتاج أيضاً — ليس اختيارياً.**
> انظر القسم التالي؛ حذفه أفشل الإصدار الحقيقي بصمت في هذا النشر فعلياً.

بيئة الاختبار لها `--cert-name` مستقل عمداً — يمنح شهادة الاختبار اسماً
منفصلاً فيُلغي الحاجة لحذف شيء قبل طلب الإنتاج (لا حذف، تسمية فقط).
البريد محسوم بـ `--register-unsafely-without-email` لكلا الطلبين —
Let's Encrypt أوقفت إشعارات انتهاء الشهادة بالبريد نهائياً، فلا قيمة
إنذارية له هنا أصلاً.

**إن فشلت أيّ محاولة: يُلصق نص الخطأ ومقتطف
`/var/log/letsencrypt/letsencrypt.log` حرفياً ويُوقَف — لا إعادة للأمر
ولا تبديل راية والمحاولة ثانية.**

#### ٦-د. الكتلة النهائية والتجديد

```bash
cp ops/nginx-hotel-admin-limits.conf /etc/nginx/conf.d/hotel-admin-limits.conf
cp ops/nginx-hotel-admin.conf /etc/nginx/sites-available/hotel-admin
ln -sf /etc/nginx/sites-available/hotel-admin /etc/nginx/sites-enabled/hotel-admin
install -m 0755 ops/letsencrypt-reload-nginx.sh \
  /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
nginx -t && systemctl reload nginx
```

- كتلة ٤٤٣ تشير إلى `/etc/letsencrypt/live/149.104.71.71/{fullchain,privkey}.pem`،
  وكتلة ٨٠ تحوّل كل شيء إلى HTTPS عدا `.well-known/acme-challenge`
  (لتبقى قناة التجديد مفتوحة دائماً — انظر النقطة الحرجة أدناه).
- `limit_req` على `/login` فقط (`rate=10r/m burst=5 nodelay`، المنطقة
  معرَّفة في `conf.d` لأن `limit_req_zone` يحتاج سياق `http{}`) — اللوحة
  على عنوان عام بلا جدار جهاز، وصفحة دخول بلا حدّ معدّل هدف مجاني.
- **بلا HSTS، عن قصد.** انظر النقطة الحرجة عن عمر الشهادة أدناه — أي
  تعثّر تجديد مع HSTS يقفل المتصفح على الموقع بلا مخرج.
- `--deploy-hook` إجباري: إضافة nginx في certbot **لا تُركِّب شهادات
  IP**، فلا تعيد تحميل nginx من تلقاء نفسها بعد التجديد.

**تحقق نهائي:**
```bash
openssl s_client -connect 149.104.71.71:443 </dev/null | openssl x509 -noout -subject -issuer -dates
certbot renew --dry-run
systemctl list-timers | grep certbot
```
ومن جهازك (لا الخادم): `curl.exe http://149.104.71.71:3000/` يجب أن
**يفشل الاتصال** — هذا إثبات أن المنفذ غير مكشوف، لا افتراض من قراءة
ملف الوحدة.

---

## ثلاث نقاط تشغيلية حرجة

### ١. مطابقة certbot للشهادات القائمة تعمل بالمعرّف (IP)، لا بالاسم

`certbot certonly` بلا `--cert-name` صريح **لا ينشئ بالضرورة شهادة
جديدة حتى لو الاسم الافتراضي غير مستخدم بعد.** يبحث أولاً عن أي شهادة
محلية قائمة تغطي نفس مجموعة المعرِّفات (هنا: العنوان `149.104.71.71`
وحده)، بصرف النظر عن اسمها. حدث فعلياً في هذا النشر: بعد إصدار شهادة
الاختبار باسم `staging-149.104.71.71`، طلب الإنتاج بلا `--cert-name`
وجد تلك الشهادة تغطي نفس المعرّف، راجع نظام ARI (Automated Renewal
Information) لدى Let's Encrypt، فجاءه أنها "غير مستحقة للتجديد بعد"،
فقرر:

```
Keeping the existing certificate
Certificate not yet due for renewal; no action taken.
```

**بلا أي رسالة خطأ — الخروج ٠.** النتيجة: شهادة اختبار (`TEST_CERT`،
غير موثوقة للمتصفح) بقيت مكان شهادة إنتاج حقيقية كانت متوقَّعة، ولم
يُستهلك أي طلب فعلي من حصة الإنتاج (لم يصل الأمر لتقديم طلب `newOrder`
أصلاً — الفحص توقف قبله).

**القاعدة الثابتة:** أي طلب `certonly`/`renew` مستقبلي على هذا الخادم
لهذا المعرّف — سواء بيئة اختبار أو إنتاج — **يمرّر `--cert-name` صريحاً
دائماً**، ولا يُترَك للاشتقاق التلقائي. هذا ما يفصل بين اللينيجات
(lineages) فعلياً، لا مجرد التسمية الظاهرة في `certbot certificates`.

### ٢. شهادة IP قصيرة العمر — ٦ أيام لا ٩٠، ومؤقّت التجديد أخطر نقطة هنا

شهادات Let's Encrypt لعناوين IP تصدر ببروفايل `shortlived` إجبارياً —
**صلاحيتها الفعلية على هذا الخادم ٦ أيام** (`notAfter` = وقت الإصدار +
٦ أيام تقريباً)، لا ٩٠ يوماً المعتادة لشهادات النطاقات. هذا يقلب أولوية
المراقبة: على شهادة ٩٠ يوماً، مؤقّت تجديد يعمل مرة يومياً هامش أمان
مريح. هنا، **أي توقف للمؤقّت أو فشل صامت للتجديد يترك نافذة أعطال
فعلية خلال أيام قليلة، لا أشهر.**

`snap.certbot.renew.timer` (الاسم الفعلي — ليس `certbot.timer` كما قد
يُتوقَّع من التوثيق العام) يعمل افتراضياً مرتين يومياً عبر توزيعة snap
القياسية؛ **هذا هو الحد الأدنى المقبول لشهادة بهذا العمر، لا هامشاً
إضافياً.** تحقق دوري إجباري، لا مرة واحدة عند النشر:

```bash
systemctl list-timers | grep certbot
```

إن غاب هذا السطر يوماً — الشهادة ستنتهي خلال أيام قليلة بلا إنذار
مسبق (لا بريد مسجَّل عمداً، ولا HSTS يمنع من فتح الموقع بلا شهادة لو
انتهت، وهذا اختيار مقصود لا خطأ — انظر أعلاه).

### ٣. التجديد يعتمد كلياً على أن المنفذ ٨٠ حرّ لمسار التحدي

طريقة التحقق المستخدمة هنا `--webroot`، لا `--standalone` ولا تكامل
nginx التلقائي. هذا يعني: **أي شيء يحتجز المنفذ ٨٠ لاحقاً، أو يعترض
مسار `/.well-known/acme-challenge/` قبل أن يصله nginx** (قاعدة جدار
تُضاف مستقبلاً، خدمة ثانية تُشغَّل على نفس المنفذ، تعديل في كتلة ٨٠
يحذف location التحدي بالخطأ) **سيُفشل كل تجديد لاحق بصمت** — لا خطأ
واضح وقت التغيير، فقط فشل عند محاولة التجديد التالية، وعندها الشهادة
قد تكون قريبة من الانتهاء فعلاً (نافذة ٦ أيام، ليس ٩٠).

**قاعدة ثابتة لأي تعديل مستقبلي على كتلة ٨٠ في `hotel-admin`:** `location
/.well-known/acme-challenge/ { root /var/www/certbot; }` يبقى أول
location في الكتلة، قبل أي `return`/`rewrite` عام، ولا يُحذف ولا
يُعاد ترتيبه خلف كتلة أخرى. `nginx -t` وحده لا يكشف هذا الخطأ — الإعداد
صحيح نحوياً حتى لو location التحدي محجوب منطقياً خلف كتلة أعمّ.

---

## `ops/deploy.sh` — النشر اللاحق

```bash
scp ops/deploy.sh root@149.104.71.71:/root/deploy.sh
ssh root@149.104.71.71 'chmod +x /root/deploy.sh && /root/deploy.sh'
```

الترتيب: `git pull` ← `npm ci --ignore-scripts --cache=...` ← `npm run
build` ← `systemctl restart hotel-admin`، الثلاثة الأولى بمستخدم
`www-data` عبر `su -s`. **تنبيه:** `NEXT_PUBLIC_*` تُحقن وقت البناء لا
التشغيل — أي تغيير لاحق في `NEXT_PUBLIC_SUPABASE_URL` أو
`NEXT_PUBLIC_SUPABASE_ANON_KEY` يستلزم تحديث `/etc/hotel-admin/build.env`
يدوياً ثم إعادة بناء كاملة عبر هذا السكربت — إعادة تشغيل الخدمة وحدها
لا تكفي.

---

## ما لا يغطيه هذا النشر

- **لا مراقبة ولا تنبيه.** الحماية الوحيدة `Restart=always` ومؤقّت
  certbot — كلاهما يجب التحقق منه دورياً يدوياً (انظر النقطة الحرجة ٢).
- **لا نسخ احتياطي.** الخادم بلا حالة؛ كل شيء في Supabase. لو احترق،
  `ops/deploy.sh` بعد إعادة الإعداد اليدوي (مراحل ٠-٦) يعيد بناءه.
- **العرض على قاعدة التطوير `hotel-sales-agent-dev`.** ما يُدخله العميل
  يُكتب فيها. تأكد قبل أي عرض أنها لا تحوي تكلفة حقيقية ولا بيانات عميل.
- **`next build` يبقى خارج CI بعد هذا النشر.** أُنصح بإضافته في PR
  منفصل — خطوة واحدة تمنع تكرار "بناء الإنتاج لم يُجرَّب" لأي تغيير
  مستقبلي في `admin/`.
- **صفحات اللوحة ووسيطها بلا اختبارات آلية.** التشغيل الفعلي في المتصفح
  هو التحقق الوحيد المتاح حالياً.
