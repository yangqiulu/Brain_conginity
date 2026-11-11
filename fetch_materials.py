# -*- coding: utf-8 -*-
"""
自动备料脚本：RSS抓取 → LLM清洗压缩 → materials.json
安装位置：仓库根目录下 scripts/fetch_materials.py
环境变量（由 workflow 提供）：
  MATERIALS_API_KEY  必填：OpenAI兼容的API密钥
  MATERIALS_API_BASE 选填：API地址（默认 https://api.deepseek.com）
  MATERIALS_MODEL    选填：模型名（默认 deepseek-chat）
  MATERIALS_OUT      选填：输出路径（默认 materials.json，需与HTML同目录）
说明：所有大模型服务商只要是OpenAI兼容接口都可用，例如：
  DeepSeek: base=https://api.deepseek.com          model=deepseek-chat
  智谱GLM:  base=https://open.bigmodel.cn/api/paas/v4  model=glm-4-flash（免费）
  Kimi:     base=https://api.moonshot.cn/v1        model=moonshot-v1-8k
"""
import os, re, json, time
import requests, feedparser, trafilatura

# ============ 信源配置（按需增删，key必须与HTML里的标签一致） ============
FEEDS = {
    '政策':   ['https://feeds.bbci.co.uk/zhongwen/simp/rss.xml'],
    '金融':   ['https://feeds.bbci.co.uk/news/business/rss.xml'],
    '健康':   ['https://feeds.bbci.co.uk/news/health/rss.xml'],
    '科技':   ['https://feeds.bbci.co.uk/news/technology/rss.xml'],
    '工作相关': ['https://www.sciencedaily.com/rss/matter_energy/photonics.xml',
              'https://www.sciencedaily.com/rss/matter_energy/optics.xml'],
    '英语材料': ['https://aeon.co/feed.rss'],
}
PER_CATEGORY = 2      # 每天每类抓几篇
MAX_LIBRARY  = 80      # 素材库上限（超出裁旧）
MIN_SOURCE   = 400     # 原文最短字符数（太短清洗不出600字）
KEEP_LANG    = {'英语材料': 'en'}   # 这些标签保留英文，其余一律输出中文

PROMPT_ZH = (
    '你是严谨的阅读材料编辑。把下面的新闻/文章正文压缩改写成一段600~1000字的连贯中文阅读材料。'
    '要求：忠实原文信息，绝不添加原文没有的内容；删除广告、订阅提示等无关内容；保持语言流畅书面化。'
    '只输出材料正文本身，不要标题、不要任何解释。\n\n原文：\n'
)
PROMPT_EN = (
    'You are a meticulous reading-material editor. Rewrite the article below into a fluent, '
    'coherent English reading passage of 350-600 words. Stay strictly faithful to the source; '
    'do not add facts. Remove any promotional or irrelevant content. '
    'Output only the passage itself, no title, no explanations.\n\nSource:\n'
)

def strip_html(s):
    return re.sub(r'<[^>]+>', ' ', s or '')

def llm_clean(text, tag):
    key = os.environ.get('MATERIALS_API_KEY', '').strip()
    if not key:
        print('警告：未设置MATERIALS_API_KEY，本篇跳过LLM清洗')
        return None
    base = os.environ.get('MATERIALS_API_BASE', 'https://api.deepseek.com').strip().rstrip('/')
    model = os.environ.get('MATERIALS_MODEL', 'deepseek-chat').strip()
    prompt = PROMPT_EN if KEEP_LANG.get(tag) == 'en' else PROMPT_ZH
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': 'You are a careful reading-material editor.'},
            {'role': 'user', 'content': prompt + text[:9000]},
        ],
        'temperature': 0.3,
    }
    try:
        r = requests.post(
            base + '/chat/completions',
            headers={'Authorization': 'Bearer ' + key},
            json=payload, timeout=180,
        )
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content'].strip()
    except Exception as e:
        print('LLM调用失败：', str(e)[:200])
        return None

def fetch_full(url):
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return ''
        return trafilatura.extract(downloaded) or ''
    except Exception:
        return ''

def main():
    out_path = os.environ.get('MATERIALS_OUT', 'materials.json')
    data = {'materials': [], 'srcs': []}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding='utf-8') as f:
                data = json.load(f)
        except Exception:
            data = {'materials': [], 'srcs': []}
    seen = set(data.get('srcs', []))
    for it in data.get('materials', []):
        if it.get('src'):
            seen.add(it['src'])
    new_items = []
    for tag, urls in FEEDS.items():
        got = 0
        for url in urls:
            if got >= PER_CATEGORY:
                break
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                print('RSS失败', url, str(e)[:100])
                continue
            for e in feed.entries:
                if got >= PER_CATEGORY:
                    break
                link = getattr(e, 'link', '')
                if not link or link in seen:
                    continue
                seen.add(link)
                raw = strip_html(getattr(e, 'summary', ''))
                full = fetch_full(link)
                if len(full) > len(raw):
                    raw = full
                if len(raw) < MIN_SOURCE:
                    print('原文太短，跳过：', link[:80])
                    continue
                cleaned = llm_clean(raw, tag)
                if not cleaned or len(cleaned) < 300:
                    print('清洗失败，跳过：', link[:80])
                    continue
                new_items.append({
                    'tag': tag,
                    'title': getattr(e, 'title', '')[:120],
                    'src': link,
                    'text': cleaned,
                })
                got += 1
                print('已清洗 [%s] %s' % (tag, getattr(e, 'title', '')[:50]))
                time.sleep(2)
    materials = (data.get('materials', []) + new_items)[-MAX_LIBRARY:]
    srcs = list(seen)[-500:]
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({
            'updated': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'materials': materials,
            'srcs': srcs,
        }, f, ensure_ascii=False, indent=1)
    print('本次新增 %d 篇，素材库共 %d 篇' % (len(new_items), len(materials)))

if __name__ == '__main__':
    main()
