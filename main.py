from demo1 import pdf_parse_main
from pathlib import Path
from tqdm import tqdm
from pebble import ProcessPool


def run_transform(pdfs):
    with ProcessPool(max_workers=10) as pool:
        tasks = {}
        for pdf in pdfs:
            fname = pdf.stem
            tasks[fname] = pool.schedule(
                pdf_parse_main,
                args=[pdf],
                kwargs={'parse_method': "txt"},
                timeout=20 * 60,
            )

        for fname in tqdm(tasks):
            try:
                res = tasks[fname].result()
            except TimeoutError:
                print(f"{fname} timed out")


if __name__ == '__main__':
    pdfs = [pdf for pdf in Path('./').glob('*.pdf')]
    #run_transform(pdfs)
    pdf_parse_main('an_fgv_adm_2012_1o_semestre_mat_aplicada.pdf', parse_method="txt")
