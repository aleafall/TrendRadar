import os
import sqlite3
import datetime
import boto3
import google.generativeai as genai
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

# ---------------- 配置区域 ----------------
# 从环境变量获取密钥
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 邮件配置 (假设你复用原项目的邮件配置环境变量)
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.qq.com") # 默认示例，请根据实际修改
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# ---------------- 1. 从 R2 下载数据 ----------------
def download_db():
    # 获取北京时间 (UTC+8)
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = beijing_time.strftime("%Y-%m-%d")
    file_key = f"news/{date_str}.db"
    local_filename = "daily_news.db"

    print(f"正在尝试下载: {file_key}")

    s3 = boto3.client(
        's3',
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY
    )

    try:
        s3.download_file(S3_BUCKET_NAME, file_key, local_filename)
        print("数据库下载成功。")
        return local_filename
    except Exception as e:
        print(f"下载失败 (可能是今天的数据还没生成?): {e}")
        return None

# ---------------- 2. 读取 SQLite 数据 ----------------
def extract_news(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # 动态获取表名 (防止表名变动)
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    if not tables:
        return ""
    
    # 假设第一个表就是存数据的表 (通常是 'news' 或 'hot_search')
    table_name = tables[0][0]
    
    # 获取最近的数据，限制条数避免 Token 溢出 (例如取最近的 200 条标题)
    # 假设有 title 字段，如果结构不同需调整
    try:
        cursor.execute(f"SELECT title FROM {table_name} ORDER BY rowid DESC LIMIT 200")
        rows = cursor.fetchall()
        news_text = "\n".join([f"- {row[0]}" for row in rows])
        return news_text
    except Exception as e:
        print(f"读取数据失败: {e}")
        return ""
    finally:
        conn.close()

# ---------------- 3. Gemini AI 分析 ----------------
def analyze_with_gemini(news_content):
    if not news_content:
        return "今日暂无足够数据进行分析。"

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash') # 使用 flash 模型，速度快且免费额度高

    # 获取当前时间段 (下午 or 晚上)
    beijing_hour = (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).hour
    time_period = "晚间总结" if beijing_hour >= 18 else "午间速览"

    prompt = f"""
    你是一个专业的新闻主编。以下是今天截止目前的网络热搜和新闻标题集合。
    请帮我生成一份**{time_period}**。

    要求：
    1. **排版美观**：使用 Emoji、Markdown 标题、分割线进行排版。
    2. **核心分类**：将新闻归类（例如：🔥 舆论热点、💻 科技前沿、💰 财经动态、🎬 娱乐/生活）。
    3. **深度总结**：不要只列标题，对最热门的 3-5 个事件进行一句话的深度解读或背景补充。
    4. **语气风格**：客观、简洁、富有洞察力。
    5. **HTML格式**：请直接输出适用于邮件发送的 HTML 源码（包含内联 CSS 样式，确保在手机上阅读体验良好），不要输出 Markdown 代码块标记。

    数据如下：
    {news_content}
    """

    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI 分析失败: {e}"

# ---------------- 4. 发送邮件 ----------------
def send_email(content):
    if not SMTP_USER or not EMAIL_TO:
        print("未配置邮件环境变量，跳过发送。")
        print("--- 生成的内容如下 ---")
        print(content)
        return

    msg = MIMEMultipart()
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    subject_time = beijing_time.strftime("%m月%d日")
    subject_period = "晚间回顾" if beijing_time.hour >= 18 else "午间速递"
    
    msg['Subject'] = Header(f"【TrendRadar AI】{subject_time} {subject_period}", 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = EMAIL_TO

    # 假设 Gemini 返回的是 HTML
    msg.attach(MIMEText(content, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        server.quit()
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败: {e}")

# ---------------- 主程序 ----------------
if __name__ == "__main__":
    db_file = download_db()
    if db_file:
        raw_news = extract_news(db_file)
        if raw_news:
            print("正在进行 AI 分析...")
            ai_summary = analyze_with_gemini(raw_news)
            send_email(ai_summary)
        else:
            print("数据库为空或无法读取。")
    
    # 清理临时文件
    if db_file and os.path.exists(db_file):
        os.remove(db_file)