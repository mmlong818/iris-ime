"""GLM 润色: 口语转写稿 → 可直接发送的成稿。

需求 R1: 口头禅(呃/那个/然后)剔除, 标点分段编号加好,
改口("用 Postgres,不对,用 SQLite")只留终版。
需求 R6(Phase 2): 直译模式说中出英。

设计: OpenAI 兼容 /chat/completions; 超时/出错抛异常 → 调用方回退原文。
temperature 低(0.2): 整理不是创作, 忠实优先。
"""

BASE_RULES = """你是听写整理器。把用户口述的转写稿整理成可直接发送的成稿。规则:
1. 删除口头禅与填充词(呃、嗯、那个、然后、就是、对吧等), 不改变原意
2. 口述里的改口、犹豫、自我纠正只保留最终说法——
   例: "用 Postgres,不对,用 SQLite" → 输出只含 SQLite
3. 补全标点; 一段意思超过三句时合理分段
4. 口头说"第一/第二/首先/其次"或列举多项时, 整理成编号列表
5. 专有名词按常识规范写法(如"SQL"不写成"sequel")
6. 只输出整理后的正文, 不要解释、不要前后缀、不要 markdown 代码块
7. 整理不等于扩写: 不得添加口述中没有的信息"""

PROFILE_RULES = {
    "default": "",
    "coding": "\n8. 场景是 AI 编程助手的指令: 多个诉求整理成带编号的需求清单, 每条一句话说清要什么",
    "email": "\n8. 场景是邮件正文: 开头补称呼('您好'/收件人名可从口述推断, 没有则用'您好'), 结尾补落款('祝好'类), 语气正式",
    "chat": "\n8. 场景是即时聊天(微信): 保留口语和语气, 只去口头禅和错字, 不要正式化, 尽量短",
}


def build_prompt(text, profile="default"):
    rules = BASE_RULES + PROFILE_RULES.get(profile, "")
    return {"role": "user", "content": f"{rules}\n\n口述转写稿:\n{text}"}


def build_translate_prompt(text):
    return {"role": "user", "content":
            f"把下面的中文口述准确翻译成自然的英文, 只输出译文:\n{text}"}


class Polisher:
    """GLM 聊天补全客户端(OpenAI 兼容)。"""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def _chat(self, messages):
        import requests
        cfg = self.cfg
        resp = requests.post(
            f"{cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}"},
            json={"model": cfg["model"],
                  "messages": [{"role": "system",
                                "content": "你是忠实的听写整理助手。"}] + messages,
                  "temperature": cfg.get("temperature", 0.2),
                  "stream": False},
            timeout=cfg.get("timeout", 20))
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    def polish(self, text, profile="default"):
        return self._chat([build_prompt(text, profile)])

    def translate(self, text):
        return self._chat([build_translate_prompt(text)])
