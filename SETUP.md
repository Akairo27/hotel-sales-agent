# دليل البدء

اقرأها بالترتيب. بعد كل جزء فيه **✅ تحقق** — لا تكمل قبل ما تتأكد منه.

---

# الجزء ٠ — خريطة سريعة

أربع خطوات:

1. تثبيت الأدوات
2. تجهيز مجلد المشروع وملفاته
3. رفعه على GitHub
4. تشغيل Claude Code وإعطاؤه أول مهمة

**الوقت المتوقع:** أقل من ساعة.

---

# الجزء ١ — الأدوات

### ١.١ تحقق مما هو مثبّت

```bash
git --version
python --version
node --version
```

الناقص نزّله:
- Git → `git-scm.com`
- Python 3.12+ → `python.org`
- Node 22+ → `nodejs.org`

### ١.٢ GitHub CLI

نزّله من `cli.github.com`، ثم:

```bash
gh auth login
```

اختر: GitHub.com ← HTTPS ← Login with a web browser.
يفتح المتصفح ويطلب رمزاً معروضاً في الطرفية — الصقه واعتمد.

### ١.٣ Claude Code

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

### ✅ تحقق

```bash
git --version
gh auth status
claude --version
```

الثلاثة بدون أخطاء؟ كمّل.

---

# الجزء ٢ — مجلد المشروع

### ٢.١ أنشئه

```bash
mkdir hotel-sales-agent
cd hotel-sales-agent
git init
mkdir .claude
```

### ٢.٢ انسخ الملفات وأعد تسميتها

| الملف المنزّل | اسمه ومكانه |
|---|---|
| `gitignore.txt` | `.gitignore` |
| `env.example.txt` | `.env.example` |
| `settings.json` | `.claude/settings.json` |
| `CLAUDE.md` | `CLAUDE.md` |
| `ARCHITECTURE.md` | `ARCHITECTURE.md` |
| `PLAN.md` | `PLAN.md` |
| `SETUP.md` | `SETUP.md` |

**وثيقة النطاق لا تدخل المجلد** — هذي للعميل.

الشكل النهائي:

```
hotel-sales-agent/
├── .claude/
│   └── settings.json
├── .gitignore
├── .env.example
├── CLAUDE.md
├── ARCHITECTURE.md
├── PLAN.md
└── SETUP.md
```

### ✅ تحقق

```bash
ls -a
```
تشوف `.claude` و `.gitignore`؟ تمام.

---

# الجزء ٣ — GitHub

### ٣.١ أول commit

```bash
git add .
git status
```

**اقرأ المخرجات بعناية.** لازم تشوف السبعة ملفات فقط.
لو ظهر `.env` أو أي ملف أسرار — **توقف** وراجع `.gitignore`.

```bash
git commit -m "chore: project foundation"
```

### ٣.٢ أنشئ المستودع

```bash
gh repo create hotel-sales-agent --private --source=. --remote=origin --push
```

`--private` مهمة. لا تتركها.

### ✅ تحقق

```bash
gh repo view --web
```

تأكد إن مكتوب **Private** جنب الاسم، وإن الملفات موجودة.

### ٣.٣ حماية main

من صفحة المستودع: **Settings** ← **Branches** ← **Add branch protection rule**

- Branch name pattern: `main`
- ✅ Require a pull request before merging
- ✅ Block force pushes
- **Create**

**تحقق:** جرّب `git push origin main` — لازم يرفض.

---

# الجزء ٤ — Claude Code

### ٤.١ شغّله

من داخل مجلد المشروع:

```bash
claude
```

أول مرة يطلب تسجيل دخول. اتبع التعليمات.

### ٤.٢ اختر النموذج

داخل Claude Code اكتب:

```
/model
```

يعرض لك المتاح في حسابك. اختر **opusplan** إن كان موجوداً — يخطط بـ Opus وينفّذ بـ Sonnet.
إن لم يكن موجوداً، اختر **Sonnet**. كافٍ تماماً للمرحلتين ٠ و١.

### ٤.٣ أول أمر

الصق هذا بالضبط:

```
اقرأ CLAUDE.md و ARCHITECTURE.md و PLAN.md بالكامل.

نفّذ المرحلة ٠ فقط من PLAN.md:
- هيكل المجلدات كما في CLAUDE.md
- إعداد ruff و mypy --strict و pytest مع حدود التغطية
- ملف .pre-commit-config.yaml مع فحص الأسرار
- GitHub Actions workflow يشغّل كل الفحوصات على كل PR

لا تكتب أي كود منتج. لا تنشئ أي ترحيل قاعدة بيانات. لا تنشئ أي خدمة.

اشتغل على فرع chore/phase-0 وقل لي لما تخلص.
```

### ٤.٤ أثناء الشغل

- راح يوقف ويستأذنك عند بعض الأوامر — هذا مقصود
- لو حاول يسوي شي خارج المطلوب: **"ارجع لـ PLAN.md، المرحلة ٠ فقط."**
- استخدم `/clear` قبل ما تبدأ مهمة جديدة

---

# الجزء ٥ — اختبار البوابات

**لا تتخطاها.** بدونها ما تدري إذا الحماية شغالة أصلاً.

```bash
git checkout -b test/gates
```

أنشئ ملف `tests/verify_gates.py`:

```python
def bad(x):
    api_key = "sk-test-1234567890abcdef"
    try:
        return x / 0
    except:
        pass
```

ثلاث مخالفات متعمدة: دالة بلا أنواع، سر مكشوف، `except` عارية.

```bash
git add . && git commit -m "test: verify gates"
git push origin test/gates
```

افتح PR من GitHub.

### ✅ النتيجة المطلوبة

**البناء لازم يفشل** — علامة ❌ حمراء.

- فشل؟ البوابات شغالة. أغلق الـ PR واحذف الفرع.
- نجح؟ **البوابات وهمية.** أصلحها قبل أي شي ثاني.

---

# الجزء ٦ — قاعدة البيانات

تحتاجها في **المرحلة ١**، مو الحين.

بما إننا ما نستخدم Docker، Supabase المحلي ما يشتغل (يحتاج Docker). البديل:

**أنشئ مشروع Supabase مجاني للتطوير** من `supabase.com`:
- مشروع منفصل تماماً عن الإنتاج
- بيانات وهمية فقط
- **لا تدخل فيه أي تكلفة حقيقية ولا بيانات عميل**

خذ منه `DATABASE_URL` والمفاتيح، وحطها في `.env` عندك.

لاحقاً عند الإنتاج، تنشئ مشروعاً ثانياً منفصلاً.

### قاعدة بيانات الاختبار المحلية (TEST_DATABASE_URL)

لتشغيل حزمة الاختبارات الكاملة (`pytest`) ضد Postgres حقيقي محلياً،
`TEST_DATABASE_URL` **لازم يتصل بدور `postgres`**، لا دورك الشخصي:

```
postgresql://postgres@localhost:5432/hotel_sales_test
```

**السبب:** قواعد `ALTER DEFAULT PRIVILEGES` في ميقريشن 0012 مقيّدة بـ
`FOR ROLE postgres` تحديداً (راجع ARCHITECTURE.md §8). من يتصل بدوره
الشخصي (مثلاً عبر مقبس يونكس بهوية نظام التشغيل) تفشل عنده ست اختبارات
في `tests/integration/test_default_privileges_lockdown.py` و
`tests/integration/test_customer_erasure.py` برسالة `DID NOT RAISE` —
ويوهم هذا أن حماية تنفيذ الدوال مكسورة، وهي سليمة فعلاً. الـ CI يتصل
بدور `postgres` دائماً، فهذا الفشل محلي بحت ولا يعني وجود مشكلة حقيقية
في المستودع.

---

# الجزء ٧ — دورة العمل اليومية

```bash
# ١. فرع جديد
git checkout main
git pull
git checkout -b feat/اسم-المهمة

# ٢. شغّل Claude Code على مهمة واحدة محددة

# ٣. راجع بنفسك
git diff

# ٤. ارفع
git add .
git commit -m "feat: وصف قصير"
git push origin feat/اسم-المهمة

# ٥. افتح PR من GitHub، راجع، ادمج

# ٦. نظّف
git checkout main
git pull
```

**قواعد ثابتة:**
- مهمة واحدة لكل فرع
- `/clear` بين المهام
- لا تدمج والبناء أحمر
- راجع بنفسك أي تغيير في `pricing/` أو `inventory/` سطراً سطراً

---

# الجزء ٨ — أخطاء شائعة

| المشكلة | الحل |
|---|---|
| `claude: command not found` | أعد التثبيت، وأعد فتح الطرفية |
| `git push` مرفوض على main | صحيح ومقصود — أنشئ فرعاً وافتح PR |
| `gh: command not found` | GitHub CLI مو مثبّت |
| Claude Code يطلع خارج المهمة | أوقفه: "ارجع لـ PLAN.md، المرحلة ٠ فقط" |
| الحصة خلصت بسرعة | استخدم `/clear` بين المهام، وSonnet بدل Opus |

**لو وقفت، ارجع للمحادثة بالخطأ كاملاً.**

---

# ملخص

```
١. ثبّت: Git, GitHub CLI, Python, Node, Claude Code
٢. أنشئ المجلد وحط الملفات السبعة
٣. gh repo create --private --push
٤. فعّل حماية main
٥. claude → /model → أمر المرحلة ٠
٦. اختبر البوابات
٧. ابدأ المرحلة ١
```

**وبالتوازي:** أرسل للعميل وثيقة النطاق وقائمة الأسئلة.
