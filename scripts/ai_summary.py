import os
import sqlite3
import datetime
import boto3
from google import genai
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
SMTP_USER = os.environ.get("EMAIL_FROM")      # 对应 GitHub Secret
SMTP_PASSWORD = os.environ.get("EMAIL_PASSWORD") # 对应 GitHub Secret
EMAIL_TO = os.environ.get("EMAIL_TO")

# ---------------- 1. 从 R2 下载数据 ----------------
def download_db():
    beijing_time = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
    date_str = beijing_time.strftime("%Y-%m-%d")
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

# ---------------- 2. 读取并处理数据 (去重+URL) ----------------
def get_news_data(db_path):
    """
    返回一个字典列表，每个元素包含 title, platform_id, url
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='news_items'")
        if cursor.fetchone()[0] == 0:
            return []

        # SQL 策略：
        # 1. GROUP BY title: 针对完全相同的标题去重
        # 2. MAX(url): 如果有多个链接，取一个非空的
        # 3. MAX(crawl_count): 取最大的抓取次数作为热度
        # 4. ORDER BY heat DESC: 按热度排序
        query = """
        SELECT title, platform_id, MAX(url) as link, MAX(crawl_count) as heat 
        FROM news_items 
        GROUP BY title 
        ORDER BY heat DESC 
        LIMIT 200
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        news_list = []
        seen_titles = set() # 二次去重（用于过滤非常相似的标题，可选）

        for row in rows:
            title = row[0]
            platform = row[1]
            url = row[2]
            
            if not title or len(title) < 4:
                continue

            # 简单的相似去重：如果前面已经有了完全包含这个标题的更长的标题，或者它是之前标题的子集
            # 这里为了效率，只做简单清洗，主要依赖 SQL 的 GROUP BY
            news_list.append({
                "title": title,
                "platform": platform,
                "url": url,
                "heat": row[3]
            })
        
        print(f"成功提取 {len(news_list)} 条唯一新闻数据。")
        return news_list

    except Exception as e:
        print(f"读取数据库失败: {e}")
        return []
    finally:
        conn.close()

# ---------------- 3. Gemini AI 分析 ----------------
def analyze_with_gemini(news_list):
    if not news_list:
        return None, None

    # 1. 将列表转换为纯文本格式喂给 AI
    prompt_text = ""
    for item in news_list[:150]: # 给 AI 前 150 条即可，避免 token 过多
        prompt_text += f"[{item['platform']}] {item['title']}\n"

    print("正在初始化 Gemini Client...")
    client = genai.Client(api_key=GEMINI_API_KEY)

    candidate_models = [
        'gemini-3-pro-preview',  # 最新实验版
        'gemini-2.5-pro',  
        'gemini-2.5-flash',     
        'gemini-2.5-flash-lite'  
    ]

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

    prompt = f"""
    你是一个专业的新闻主编。以下是今日全网热搜数据。
    请生成一份 HTML 格式的**{period_title}**邮件。

    ### 要求：
    1.  **摘要部分**：从数据中提炼 5-8 个最核心、最值得关注的事件。
    2.  **内容处理**：对每个核心事件进行一句话深度简评或背景补充。
    3.  **排版要求**：
        -   仅输出摘要部分的 HTML 代码。
        -   使用内联 CSS，风格简洁现代。
        -   **不要**包含“数据来源列表”，这部分我会自己生成。
        -   **不要**包含 Markdown 标记。

    ### 待分析数据：
    {prompt_text}
    """

    for model_name in candidate_models:
        print(f"正在尝试使用模型: {model_name} ...")
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt
            )
            print(f"模型 {model_name} 调用成功！")
            text = response.text
            text = text.replace("```html", "").replace("```", "").strip()
            return text, model_name
        except Exception as e:
            print(f"模型 {model_name} 失败: {e}")
            if "404" in str(e) or "not found" in str(e).lower():
                continue
            continue

    return None, None

# ---------------- 4. 生成完整新闻列表 HTML ----------------
def generate_news_list_html(news_list):
    """
    生成一个紧凑的新闻列表 HTML 表格/列表
    """
    if not news_list:
        return ""
    
    html = """
    <div style="margin-top: 40px; border-top: 2px dashed #eee; padding-top: 20px;">
        <h3 style="color: #333; border-left: 4px solid #007bff; padding-left: 10px; margin-bottom: 15px;">
            📋 今日全网热点清单
        </h3>
        <div style="font-family: sans-serif; font-size: 13px; line-height: 1.6; color: #444;">
    """
    
    # 按平台简单分组显示可能更好看，或者直接混排
    # 这里采用直接混排（按热度），使用表格布局
    
    html += '<table style="width: 100%; border-collapse: collapse;">'
    
    for item in news_list:
        title = item['title']
        platform = item['platform']
        url = item['url']
        
        # 平台样式标记
        platform_badge = f'<span style="display:inline-block; padding:2px 6px; background:#f0f0f0; color:#666; border-radius:4px; font-size:11px; margin-right:8px; width:60px; text-align:center; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{platform}</span>'
        
        # 标题链接
        if url and url.startswith('http'):
            title_html = f'<a href="{url}" style="text-decoration: none; color: #0066cc;" target="_blank">{title}</a>'
        else:
            title_html = f'<span style="color: #333;">{title}</span>'
            
        html += f"""
        <tr style="border-bottom: 1px solid #f5f5f5;">
            <td style="padding: 8px 0; vertical-align: middle;">
                <div style="display: flex; align-items: center;">
                    {platform_badge}
                    {title_html}
                </div>
            </td>
        </tr>
        """
        
    html += """
        </table>
        </div>
    </div>
    """
    return html

# ---------------- 5. 发送邮件 ----------------
def send_email(ai_summary_html, appendix_html, model_name):
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

    # --- 拼接所有 HTML ---
    footer_html = f"""
    <div style="margin-top: 30px; padding-top: 10px; border-top: 1px solid #eee; text-align: center; font-size: 12px; color: #999; font-family: sans-serif;">
        AI Analysis generated by <strong>{model_name}</strong> • TrendRadar
    </div>
    """
    
    # 组合逻辑：AI 总结 + 完整列表 + Footer
    # 如果 AI 总结包含 </body>，则移除它以便拼接
    full_body = ai_summary_html.replace("</body>", "").replace("</html>", "")
    full_body += appendix_html
    full_body += footer_html
    full_body += "</body></html>"

    msg.attach(MIMEText(full_body, 'html', 'utf-8'))

    try:
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
        server.quit()
        print(f"邮件已成功发送至: {EMAIL_TO} (Model: {model_name})")
    except Exception as e:
        print(f"邮件发送失败: {e}")

# ---------------- 主程序 ----------------
if __name__ == "__main__":
    print("--- 开始执行 TrendRadar AI 总结 ---")
    db_file = download_db()
    
    if db_file:
        # 1. 获取结构化数据
        news_list = get_news_data(db_file)
        
        if news_list:
            # 2. AI 生成总结 (只给 AI 看前 150 条)
            ai_html, used_model = analyze_with_gemini(news_list)
            
            if ai_html and used_model:
                # 3. 生成完整附录 (显示所有 200 条)
                appendix_html = generate_news_list_html(news_list)
                
                # 4. 发送
                send_email(ai_html, appendix_html, used_model)
            else:
                print("跳过发送：AI 未返回内容。")
        else:
            print("跳过发送：未提取到有效新闻。")
        
        try:
            os.remove(db_file)
        except:
            pass
    else:
        print("跳过执行：无法下载数据库文件。")