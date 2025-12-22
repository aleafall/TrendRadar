import os
import sqlite3
import datetime
import boto3
from google import genai  # 使用新的官方 SDK
from google.genai import types
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

# ---------------- 配置区域 ----------------
S3_BUCKET_NAME = os.environ.get("S3_BUCKET_NAME")
S3_ACCESS_KEY_ID = os.environ.get("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.environ.get("S3_SECRET_ACCESS_KEY")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# 邮件配置
SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.qq.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 465))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO")

# ---------------- 1. 从 R2 下载数据 ----------------
def download_db():
    # 获取北京时间
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = beijing_time.strftime("%Y-%m-%d")
    
    # 路径格式: news/2025-12-21.db
    file_key = f"news/{date_str}.db"
    local_filename = "daily_news.db"

    print(f"[{beijing_time.strftime('%H:%M')}] 正在从 R2 下载: {file_key}")

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
        print(f"下载失败 (可能是今天尚未生成数据): {e}")
        return None

# ---------------- 2. 读取 SQLite 数据 ----------------
def extract_hot_news(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # 检查表是否存在
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='news_items'")
        if cursor.fetchone()[0] == 0:
            print("错误：数据库中找不到 news_items 表")
            return ""

        # SQL 查询策略：
        # 1. 按 title 分组 (去除不同时间点的重复抓取)
        # 2. 取 MAX(crawl_count) 作为热度指标
        # 3. 倒序排列，取前 150 条最持久的热点
        query = """
        SELECT title, platform_id, MAX(crawl_count) as heat 
        FROM news_items 
        GROUP BY title 
        ORDER BY heat DESC 
        LIMIT 150
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        if not rows:
            print("数据库中没有数据行。")
            return ""

        # 格式化数据给 AI
        news_lines = []
        for row in rows:
            title = row[0]
            platform = row[1]
            # 简单清洗：过滤掉过短的标题
            if title and len(title) > 4:
                news_lines.append(f"[{platform}] {title}")
        
        print(f"成功提取 {len(news_lines)} 条高热度新闻。")
        return "\n".join(news_lines)

    except Exception as e:
        print(f"读取数据库失败: {e}")
        return ""
    finally:
        conn.close()

# ---------------- 3. Gemini AI 分析 (新版 SDK) ----------------
def analyze_with_gemini(news_content):
    if not news_content:
        return None

    print("正在初始化 Gemini Client...")
    # 使用新的 google-genai 客户端
    client = genai.Client(api_key=GEMINI_API_KEY)

    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    hour = beijing_time.hour
    
    if hour < 12:
        period_title = "早报"
        greeting = "新的一天，来看看昨夜今晨的热点。"
    elif hour < 18:
        period_title = "午间速览"
        greeting = "忙碌之余，为您梳理最新的网络动态。"
    else:
        period_title = "晚间回顾"
        greeting = "结束了一天的工作，为您总结今日全网焦点。"

    # 提示词
    prompt = f"""
    你是一个专业的新闻主编。以下是今日全网（包含微博、知乎、外媒等）的热搜数据。
    请根据这些数据，生成一份 HTML 格式的**{period_title}**邮件。

    ### 要求：
    1.  **筛选核心**：从列表中提炼出 5-8 个最值得关注的事件，不要简单罗列。
    2.  **分类明确**：例如【🌏 全球/时政】、【💰 财经/科技】、【🔥 社会/舆论】。
    3.  **深度一句话**：对每个标题进行一句话的背景扩充或深度锐评。
    4.  **排版要求**：
        -   **仅输出 HTML 代码**，不要包含 markdown (```html) 标记。
        -   使用内联 CSS (Inline CSS)，确保邮件显示美观。
        -   风格：卡片式设计，字体易读，背景清爽。
    5.  **结构**：
        -   标题：H2 标签，包含日期。
        -   导语：{greeting}
        -   正文：分类卡片。
        -   结尾：简短结语。

    ### 数据源：
    {news_content}
    """

    try:
        print("正在发送请求给 Gemini...")
        # 新版 API 调用方式
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt
        )
        
        text = response.text
        # 清理可能存在的 Markdown 标记 (以防万一)
        text = text.replace("```html", "").replace("```", "").strip()
        return text

    except Exception as e:
        print(f"AI 生成失败: {e}")
        return None

# ---------------- 4. 发送邮件 ----------------
def send_email(html_content):
    if not SMTP_USER or not EMAIL_TO:
        print("未配置邮件环境变量，跳过发送。")
        return

    msg = MIMEMultipart()
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = beijing_time.strftime("%m月%d日")
    
    hour = beijing_time.hour
    period = "晚间" if hour >= 18 else "午间"
    
    msg['Subject'] = Header(f"TrendRadar {date_str} {period} AI简报", 'utf-8')
    msg['From'] = SMTP_USER
    msg['To'] = EMAIL_TO

    msg.attach(MIMEText(html_content, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        server.quit()
        print(f"邮件已成功发送至: {EMAIL_TO}")
    except Exception as e:
        print(f"邮件发送失败: {e}")

# ---------------- 主程序 ----------------
if __name__ == "__main__":
    print("--- 开始执行 TrendRadar AI 总结 ---")
    
    # 1. 下载
    db_file = download_db()
    
    if db_file:
        # 2. 提取
        raw_news = extract_hot_news(db_file)
        
        if raw_news:
            # 3. 分析
            html_report = analyze_with_gemini(raw_news)
            
            if html_report:
                # 4. 发送
                send_email(html_report)
            else:
                print("跳过发送：AI 未返回内容。")
        else:
            print("跳过发送：未提取到有效新闻。")
        
        # 清理临时文件
        try:
            os.remove(db_file)
        except:
            pass
    else:
        print("跳过执行：无法下载数据库文件。")