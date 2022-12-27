import markdown, glob, os

input_path = "blogs-md"
output_path = "blogs"

papers = glob.glob(input_path + "/*")

for paper in papers:
    title = os.path.basename(paper)
    md_path = os.path.join(input_path, title, title + ".md")
    with open(md_path, "r") as f:
        text = f.read()
        html = markdown.markdown(text)
        with open(output_path+"/"+title+".html", "w") as fw:
            fw.write(html)