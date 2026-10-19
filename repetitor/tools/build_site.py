#!/usr/bin/env python3
"""Собирает статический сайт «Репетитор» (GitHub Pages) из файлов тестов.

    python3 repetitor/tools/build_site.py      # repetitor/tests/*.json  →  docs/

Берёт шаблон страницы (tutor.html из скилла или docs/tutor.html), вставляет
статическую «заглушку» базы данных и раскладывает тесты по docs/data/.
Страница работает без входа в Claude: тесты читаются из JSON-файлов,
ответы хранятся на устройстве, результат отправляется родителю кнопкой.
"""
import json, os, re, sys, glob, shutil, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # …/repetitor
REPO = os.path.dirname(ROOT)                                           # корень репозитория
TESTS = os.path.join(ROOT, "tests")
DOCS = os.path.join(REPO, "docs")                                      # GitHub Pages: ветка main, папка /docs
CONFIG = os.path.join(ROOT, "config.json")
TEMPLATE_CANDIDATES = [
    "/mnt/skills/plugins/repetitor/assets/tutor.html",
    os.path.join(ROOT, "tools", "tutor.html"),
]

SHIM = r"""
<script>
/* ===== static mode (GitHub Pages): база данных заменена JSON-файлами ===== */
(function(){
  const DATA = "data/";
  let index = null, cache = {};
  async function loadIndex(){
    if(index) return index;
    const r = await fetch(DATA + "index.json", {cache:"no-cache"});
    if(!r.ok) throw new Error("index.json " + r.status);
    index = await r.json(); return index;
  }
  async function loadQuestions(testId){
    if(cache[testId]) return cache[testId];
    const r = await fetch(DATA + "tests/" + encodeURIComponent(testId) + ".json", {cache:"no-cache"});
    if(!r.ok) throw new Error("questions " + r.status);
    const d = await r.json(); cache[testId] = d.questions || []; return cache[testId];
  }
  const snapDoc = (exists, data) => ({exists, data: () => data});
  const snapQuery = rows => ({docs: rows.map(x => ({id: x.id, data: () => { const {id, ...b} = x; return b; }}))});
  const readOnly = () => Promise.reject({code: "invalid_argument", message: "static site"});
  function db(){
    return {
      doc(path){
        return {
          onSnapshot(cb, err){ loadIndex().then(ix => {
              if(path === "config/main") cb(snapDoc(true, {kids: ix.kids}));
              else cb(snapDoc(false, null));
            }).catch(err || console.warn); },
          get(){ return loadIndex().then(ix => path === "config/main" ? snapDoc(true, {kids: ix.kids}) : snapDoc(false, null)); },
          set: readOnly, update: readOnly
        };
      },
      collection(path){
        const m = /^tests\/([^/]+)\/questions$/.exec(path);
        const fetchRows = async () => {
          if(path === "tests") return (await loadIndex()).tests;
          if(path === "attempts") return [];
          if(m) return (await loadQuestions(decodeURIComponent(m[1]))).slice().sort((a, b) => (a.order || 0) - (b.order || 0));
          return [];
        };
        const q = {
          get: () => fetchRows().then(snapQuery),
          orderBy(){ return q; },
          onSnapshot(cb, err){ fetchRows().then(r => cb(snapQuery(r))).catch(err || console.warn); },
          doc(){ return {id: "loc-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 8)}; }
        };
        return q;
      }
    };
  }
  window.claude = {
    use(name){
      if(name === "db") return Promise.resolve(db());
      if(name === "user") return Promise.resolve({can: async () => false});
      return Promise.resolve(null);
    }
  };
  /* прямая ссылка: ?kid=k8  или  ?kid=k8&test=<id> */
  window.addEventListener("load", () => {
    const p = new URLSearchParams(location.search);
    const kid = p.get("kid"), test = p.get("test");
    /* тест с объяснением (поле intro): пока объяснение не прочитано, вместо теста открываем его */
    const introSeen = id => { try { if(localStorage.getItem("intro-read:" + id) === "1") return true; } catch(e){} return p.get("read") === "1" && p.get("test") === id; };
    if(typeof startTest === "function"){
      const orig = startTest;
      startTest = function(k, t, a){
        if(t && t.intro && !a && !introSeen(t.id)){ location.href = t.intro; return; }
        return orig(k, t, a);
      };
    }
    if(!kid) return;
    let n = 0;
    const tick = () => {
      if(typeof S === "undefined" || !S.ready || !S.kids.some(k => k.id === kid)){ if(n++ < 60) setTimeout(tick, 150); return; }
      const t = test && S.tests.find(x => x.id === test);
      if(t && typeof startTest === "function") startTest(kid, t);
      else if(typeof go === "function") go({name: "kid", kid});
    };
    tick();
  });
})();
</script>
"""

# Кнопки отправки результата: «Поделиться», WhatsApp, Telegram, копирование.
SEND_PATCH_OLD = '  box.append(b,ta);return box;\n}'
SEND_PATCH_NEW = r'''  const row=h("div",{class:"share-row"});
  const txt=()=>ta.value;
  if(navigator.share)row.append(h("button",{class:"btn",type:"button",onclick:()=>{navigator.share({text:txt()}).catch(()=>{});}},"Поделиться"));
  row.append(h("a",{class:"btn ghost",href:"https://wa.me/?text="+encodeURIComponent(txt()),target:"_blank",rel:"noopener"},"WhatsApp"));
  row.append(h("a",{class:"btn ghost",href:"https://t.me/share/url?url="+encodeURIComponent(" ")+"&text="+encodeURIComponent(txt()),target:"_blank",rel:"noopener"},"Telegram"));
  row.append(b);
  box.append(row,ta);return box;
}'''

BANNER_OLD = '<b>Ответы сохраняются только на этом устройстве.</b> После теста нажми «Отправить результат» и перешли текст родителю — он добавит его в статистику.'
BANNER_NEW = '<b>Ответы сохраняются на этом устройстве.</b> В конце теста нажми «Поделиться» или «WhatsApp» — результат уйдёт родителю, и он добавит его в статистику.'

PARENT_OLD = 'h("p",{},"Откройте страницу в Claude под своим аккаунтом, чтобы увидеть статистику.")'

EXTRA_CSS = "<style>.share-row{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}.share-row .btn{text-decoration:none}</style>\n"


# ===== Тип вопроса "text": ребёнок вписывает ответ; проверка по смыслу, опечатки не важны =====
TEXT_HELPERS = r"""
function normT(s){return String(s??"").toLowerCase().replace(/ё/g,"е").replace(/[«»"'`.,;:!?()\[\]\-–—\/°]/g," ").replace(/\s+/g," ").trim();}
function levT(a,b){const m=a.length,n=b.length;if(!m)return n;if(!n)return m;let p=Array.from({length:n+1},(_,j)=>j);
  for(let i=1;i<=m;i++){const c=[i];for(let j=1;j<=n;j++)c[j]=Math.min(p[j]+1,c[j-1]+1,p[j-1]+(a[i-1]===b[j-1]?0:1));p=c;}return p[n];}
const FILLER_T=new Set("на в во это знаки знак съемка сьемка съемки материк материке остров острова океан океана г год году градусов градуса градус по к с со из от до и наука прибор линия линию".split(" "));
function tolT(n){return n<=4?0:n<=8?1:2;}
function textHit(resp,vars,mt){const r=normT(resp);if(!r)return false;const rt=r.split(" ");
  for(const v0 of vars){const v=normT(v0);if(!v)continue;let tol=tolT(v.replace(/ /g,"").length);if(mt!=null)tol=Math.min(tol,mt);
    if(levT(r,v)<=tol||levT(r.replace(/ /g,""),v.replace(/ /g,""))<=tol)return true;
    const n=v.split(" ").length;for(let i=0;i+n<=rt.length;i++){const rest=rt.slice(0,i).concat(rt.slice(i+n));
      if(rest.every(w=>FILLER_T.has(w))&&levT(rt.slice(i,i+n).join(" "),v)<=tol)return true;}}
  return false;}
function textMatch(q,resp){return textHit(resp,[q.answer,...(q.accept||[])],q.maxTypos);}
"""
TEXT_PATCHES = [
  ('function isRight(q,resp){\n',
   TEXT_HELPERS + 'function isRight(q,resp){\n  if(q.type==="text")return textMatch(q,resp);\n'),
  ('function whyWrong(q,resp){\n',
   'function whyWrong(q,resp){\n  if(q.type==="text"){for(const t of q.typicalWrong||[]){if(textHit(resp,[t.value,...(t.also||[])]))return t.why;}\n    return q.genericWrong||"Сравни свой ответ с разбором ниже.";}\n'),
  ('function answerText(q){\n',
   'function answerText(q){\n  if(q.type==="text")return String(q.answer);\n'),
  ('  if(q.type==="number"){\n    const inp=',
   '  if(q.type==="text"){\n    const inp=h("input",{type:"text",autocomplete:"off",autocapitalize:"off",spellcheck:"false",id:"txt-"+q.id,"aria-label":"Ответ",placeholder:"Впиши ответ",style:"width:min(420px,100%)"});\n    inp.addEventListener("input",()=>{chosen=inp.value;submit.disabled=!inp.value.trim();});\n    inp.addEventListener("keydown",e=>{if(e.key==="Enter"&&!submit.disabled)submit.click();});\n    ans.append(h("div",{class:"numrow"},inp));\n    ans.append(h("p",{class:"muted small",style:"margin-top:6px"},"Напиши ответ словами. Ошибки в написании не страшны — проверяется смысл."));\n  }else if(q.type==="number"){\n    const inp='),
  ('h("span",{class:"hand ok"},"Верно!"),h("span",{},"Посмотри решение — так закрепишь способ.")',
   'h("span",{class:"hand ok"},"Верно!"),q.type==="text"?h("span",{html:"Правильно пишется: <b>"+esc(String(q.answer))+"</b>. Посмотри разбор."}):h("span",{},"Посмотри решение — так закрепишь способ.")'),
]

def apply_text_patch(html):
    for old, new in TEXT_PATCHES:
        assert html.count(old) == 1, "text-патч: не найден или не уникален: " + old[:40]
        html = html.replace(old, new, 1)
    return html


def load_template():
    for p in TEMPLATE_CANDIDATES:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read(), p
    sys.exit("Не найден шаблон tutor.html (ожидался в скилле repetitor или tools/tutor.html)")


def build_page(html):
    assert "<script>" in html
    assert SEND_PATCH_OLD in html, "не найден блок отправки результата — шаблон изменился"
    html = html.replace(SEND_PATCH_OLD, SEND_PATCH_NEW, 1)
    html = html.replace(BANNER_OLD, BANNER_NEW, 1)
    html = apply_text_patch(html)
    # шим вставляем перед основным скриптом страницы
    i = html.index("<script>")
    html = html[:i] + EXTRA_CSS + SHIM + html[i:]
    # страница в артефакте получает «скелет» документа от платформы; на GitHub Pages добавляем его сами
    head = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            '<meta name="robots" content="noindex">\n')
    html = head + html
    if "</body>" not in html:
        html += "\n</body></html>\n"
    return html


def main():
    html, src = load_template()
    os.makedirs(os.path.join(DOCS, "data", "tests"), exist_ok=True)
    kids = json.load(open(CONFIG, encoding="utf-8"))["kids"] if os.path.exists(CONFIG) else [
        {"id": "k5", "name": "Ученик 5 класса", "grade": 5}, {"id": "k8", "name": "Ученик 8 класса", "grade": 8}]
    tests = []
    for f in sorted(glob.glob(os.path.join(TESTS, "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        t = dict(d["test"]); qs = d["questions"]
        meta = {k: t.get(k) for k in ("id", "kid", "grade", "subject", "topic", "title", "source", "demo", "intro") if t.get(k) is not None}
        meta["qCount"] = len(qs)
        meta["createdAt"] = t.get("createdAt") or datetime.datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="seconds")
        tests.append(meta)
        json.dump({"questions": qs}, open(os.path.join(DOCS, "data", "tests", t["id"] + ".json"), "w", encoding="utf-8"), ensure_ascii=False)
    tests.sort(key=lambda m: m["createdAt"], reverse=True)
    json.dump({"kids": kids, "tests": tests, "builtAt": datetime.datetime.now().isoformat(timespec="seconds")},
              open(os.path.join(DOCS, "data", "index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8").write(build_page(html))
    open(os.path.join(DOCS, ".nojekyll"), "w").close()
    if os.path.abspath(src) != os.path.abspath(os.path.join(ROOT, "tools", "tutor.html")):
        shutil.copy(src, os.path.join(ROOT, "tools", "tutor.html"))  # копия шаблона на случай отсутствия скилла
    print(f"ok: {len(tests)} тест(ов) → docs/  (шаблон: {src})")
    for m in tests:
        print(f"  {m['id']}: {m['kid']} · {m['title']} · {m['qCount']} вопр.")


if __name__ == "__main__":
    main()
