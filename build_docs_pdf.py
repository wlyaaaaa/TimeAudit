# -*- coding: utf-8 -*-
"""
TimeAudit 文档 PDF 生成器
=========================
把 README.md / 快速部署.md / 使用手册.md 转成排版精良、中文友好的 PDF。
原理：Markdown → 内嵌中文字体样式的 HTML → 用系统自带的 Microsoft Edge 无头模式打印成 PDF。
直接运行： python build_docs_pdf.py
依赖： pip install markdown （以及 Windows 自带的 Edge）
"""
import os, sys, subprocess, tempfile, shutil
import markdown

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = ["README.md", "快速部署.md", "使用手册.md"]

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

CSS = """
@page { size: A4; margin: 1.5cm 1.4cm; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei","微软雅黑","Segoe UI",sans-serif;
       font-size: 12px; line-height: 1.65; color: #222; }
h1 { font-size: 22px; color: #1f4e79; border-bottom: 3px solid #4a90d9; padding-bottom: 6px; }
h2 { font-size: 17px; color: #1f4e79; border-bottom: 1px solid #d8e2ec; padding-bottom: 4px; margin-top: 22px; }
h3 { font-size: 14px; color: #2c5f8a; margin-top: 16px; }
h4 { font-size: 13px; color: #34495e; margin-top: 12px; }
p, li { margin: 4px 0; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 10.5px; }
th, td { border: 1px solid #c4cdd6; padding: 4px 7px; text-align: left; vertical-align: top; }
th { background: #eef3f8; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }
tr, td, th { page-break-inside: avoid; }
code { background: #f0f2f4; padding: 1px 4px; border-radius: 3px;
       font-family: Consolas,"Courier New",monospace; font-size: 11px; color: #b5285a; }
pre { background: #f6f8fa; border: 1px solid #e1e4e8; border-radius: 5px; padding: 10px 12px;
      overflow-x: auto; page-break-inside: avoid; }
pre code { background: none; color: #24292e; padding: 0; }
blockquote { border-left: 4px solid #4a90d9; background: #f5f9fd; margin: 8px 0;
             padding: 5px 14px; color: #4a5568; }
a { color: #1a8a5a; text-decoration: none; }
h1, h2, h3, h4 { page-break-after: avoid; }
"""

HTML_TMPL = "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>" \
            "<style>{css}</style></head><body>{body}</body></html>"


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def md_to_pdf(edge, md_path, pdf_path, html_dir):
    with open(md_path, encoding="utf-8") as f:
        text = f.read()
    body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists", "toc"])
    html = HTML_TMPL.format(css=CSS, body=body)
    html_path = os.path.join(html_dir, "_pdfbuild_tmp.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    file_url = "file:///" + html_path.replace("\\", "/")
    # 关键：必须经 PowerShell 的 Start-Process -Wait 启动 Edge。直接用 Python subprocess 调 Edge，
    # 在已有 Edge 实例运行时会被"单例转发"机制抢走、自身空转退出 → 只产出空白 PDF。
    # 独立 user-data-dir + --no-first-run 隔离用户配置；html 放项目目录规避 headless 的 file:// 限制。
    prof = tempfile.mkdtemp(prefix="edge_pdf_")
    args = ("'--headless=new','--disable-gpu','--no-first-run','--no-pdf-header-footer',"
            "'--user-data-dir={prof}','--print-to-pdf={pdf}','{url}'").format(
                prof=prof, pdf=pdf_path, url=file_url)
    ps = ("$p=Start-Process -FilePath '{edge}' -ArgumentList {args} "
          "-PassThru -Wait -WindowStyle Hidden; exit $p.ExitCode").format(edge=edge, args=args)
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       timeout=120, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        try:
            os.unlink(html_path)
        except Exception:
            pass
        shutil.rmtree(prof, ignore_errors=True)
    return os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1024


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Convert Markdown files to PDF using Edge headless mode.")
    parser.add_argument("--dir", default=ROOT, help="Working directory containing the markdown files")
    parser.add_argument("--docs", nargs="+", default=None, help="List of markdown files to convert")
    parser.add_argument("--git", action="store_true", help="Add, commit, and push generated PDFs to GitHub")
    args = parser.parse_args()

    edge = find_edge()
    if not edge:
        print("❌ 未找到 Edge/Chrome，无法生成 PDF")
        return 1
    print("使用浏览器内核:", edge)

    target_dir = os.path.abspath(args.dir)
    if args.docs is not None:
        docs = args.docs
    else:
        if target_dir == ROOT:
            docs = DOCS
        else:
            docs = [f for f in os.listdir(target_dir) if f.lower().endswith(".md") and not f.startswith("_")]
            if not docs:
                print(f"❌ 未在目录 {target_dir} 中找到 markdown 文件")
                return 1

    ok = 0
    generated_pdfs = []
    for md in docs:
        md_path = os.path.join(target_dir, md)
        if not os.path.exists(md_path):
            print(f"  ⚠️ 跳过(不存在): {md}")
            continue
        pdf_path = os.path.join(target_dir, os.path.splitext(md)[0] + ".pdf")
        if md_to_pdf(edge, md_path, pdf_path, target_dir):
            print(f"  ✅ {md} → {os.path.basename(pdf_path)}  ({os.path.getsize(pdf_path)//1024} KB)")
            ok += 1
            generated_pdfs.append(pdf_path)
        else:
            print(f"  ❌ 生成失败: {md}")

    print(f"完成：{ok}/{len(docs)} 个 PDF")

    if args.git and ok > 0:
        print("🚀 正在将 PDF 自动上传到 GitHub...")
        pdf_names = [os.path.basename(p) for p in generated_pdfs]
        try:
            for pdf in pdf_names:
                subprocess.run(["git", "add", pdf], cwd=target_dir, check=True)
            status = subprocess.run(["git", "status", "--porcelain"], cwd=target_dir, capture_output=True, text=True, check=True)
            has_changes = False
            for line in status.stdout.splitlines():
                line = line.strip()
                if any(pdf in line for pdf in pdf_names):
                    has_changes = True
                    break

            if has_changes:
                commit_msg = f"docs: auto-generate PDFs for {', '.join(pdf_names)}"
                subprocess.run(["git", "commit", "-m", commit_msg], cwd=target_dir, check=True)
                subprocess.run(["git", "push"], cwd=target_dir, check=True)
                print("✅ 成功推送 PDF 到 GitHub!")
            else:
                print("ℹ️ 无更新，PDF 文件内容未发生变化，无需推送。")
        except subprocess.CalledProcessError as e:
            print(f"❌ Git 操作失败: {e}")
            return 1

    return 0 if ok == len(docs) else 1


if __name__ == "__main__":
    sys.exit(main())
