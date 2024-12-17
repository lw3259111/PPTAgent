import json
import os
import random
import shutil
import sys
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from glob import glob
from itertools import product
from tempfile import TemporaryDirectory
from time import sleep

import func_argparse
import pytorch_fid.fid_score as fid
import torch
from jinja2 import Template
from rich import print
from tqdm import tqdm
from transformers import GPT2LMHeadModel, GPT2TokenizerFast

import llms
from crawler import topics
from faster_pytorch_fid.fid_score_gpu import compute_statistics_of_path
from presentation import Picture, Presentation, SlidePage
from utils import Config, older_than, pexists, pjoin, ppt_to_images

fid.tqdm = lambda x: x
judges = [
    (llms.gpt4o, llms.gpt4o, "gpt4o"),
    (llms.qwen2_5, llms.intern_vl, "qwen+intern"),
    (llms.qwen2_5, llms.qwen_vl, "Qwen"),
    (llms.qwen_vl, llms.qwen_vl, "qwen_vl"),
    (llms.intern_vl, llms.intern_vl, "intern_vl"),
]
DEVICES = torch.cuda.device_count()


def get_ppl(slide: SlidePage, model: GPT2LMHeadModel, tokenizer: GPT2TokenizerFast):
    ppl = []
    text = slide.to_text()
    if len(text) == 0:
        return ppl
    tokenized = tokenizer(text, return_tensors="pt").to(model.device)
    with torch.no_grad():
        outputs = model(tokenized.input_ids, labels=tokenized.input_ids)
        loss = outputs.loss
        perplexity = torch.exp(loss)
        ppl.append(perplexity.item())
    return ppl


def eval_general(presentations: list[Presentation], evals: dict[str, list[int]]):
    for prs in presentations:
        if prs.source_file in evals["pages"]:
            continue
        evals["pages"][prs.source_file] = len(prs)
        evals["characters"][prs.source_file] = sum(
            [len(slide.to_text()) for slide in prs.slides]
        )
        evals["figures"][prs.source_file] = sum(
            [len(list(slide.shape_filter(Picture))) for slide in prs.slides]
        )


def eval_feature(
    presentations: list[Presentation],
    evals: dict,
    setting: str,
):
    device = f"cuda:{random.randint(0, DEVICES - 1)}"
    print("start scoring ppl")
    model = GPT2LMHeadModel.from_pretrained("gpt2").to(device)
    tokenizer = GPT2TokenizerFast.from_pretrained("gpt2")
    for prs in tqdm(presentations):
        try:
            if prs.source_file in evals["ppl"]:
                continue
            if (
                prs.source_file
                == "data/culture/pptx/ChemBio-in-the-HUB-public/PPTCrew_wo_SchemaInduction/SSRN-id2933553_Management of Systems Engineering and Technical Assistance of DARPA Research Programs/final.pptx"
            ):
                continue
            ppl = []
            for slide in prs.slides:
                ppl.extend(get_ppl(slide, model, tokenizer))
            if len(ppl) == 0:
                continue
            evals["ppl"][prs.source_file] = sum(ppl) / len(ppl)
        except Exception as e:
            print(e, "\n", "happended in ", prs.source_file)

    model = fid.InceptionV3([fid.InceptionV3.BLOCK_INDEX_BY_DIM[64]]).to(device)
    for ppt_folder in tqdm(sorted(glob(f"data/*/pptx/*/"))):
        if ppt_folder in evals["fid"]:
            continue
        source_folder = pjoin(ppt_folder, "source_slides")
        m1, s1 = compute_statistics_of_path(source_folder, model, 128, 64, device)
        try:
            with tempfile.TemporaryDirectory(prefix="ppteval_fid_") as temp_dir:
                for result_folder in glob(
                    pjoin(ppt_folder, f"final_images/{setting}/*")
                ):
                    folder_base = os.path.basename(result_folder)
                    for image_file in os.listdir(result_folder):
                        image_path = os.path.join(result_folder, image_file)
                        temp_image_path = os.path.join(
                            temp_dir, folder_base + "_" + image_file
                        ).replace(" ", "_")
                        shutil.copyfile(image_path, temp_image_path)
                if len(os.listdir(temp_dir)) < 10:
                    continue
                m2, s2 = compute_statistics_of_path(temp_dir, model, 32, 64, device)

                evals["fid"][ppt_folder] = fid.calculate_frechet_distance(
                    m1, s1, m2, s2
                )
        except Exception as e:
            print(e, "\n", "happended in ", ppt_folder, "on:", setting)


def merge_evals(folders: list[str], evals: dict):
    for folder in folders:
        sub_eval = json.load(open(pjoin(folder, "evals.json")))
        for dimension in ["content", "vision", "logic"]:
            evals[dimension] |= sub_eval[dimension]
    return evals


def eval_ppt(
    prs_source: str = None,
    slide_folder: str = None,
):
    if prs_source is None:
        slide_folder = os.path.dirname(slide_folder)
    if slide_folder is None:
        source, setting, pdf, _ = prs_source.rsplit("/", 3)
        slide_folder = os.path.join(source, "final_images", setting, pdf)
    eval_file = pjoin(slide_folder, "evals.json")
    evals = defaultdict(dict)
    if pexists(eval_file):
        try:
            evals |= json.load(open(eval_file))
        except:
            pass
    evals.pop("logic", None)
    config = Config("/tmp")
    presentation = Presentation.from_file(prs_source, config)
    # text_scorer = Template(open("prompts/ppteval_content.txt", "r").read())
    # vision_scorer = Template(open("prompts/ppteval_style.txt", "r").read())
    # style_descriptor = open("prompts/ppteval_describe_style.txt", "r").read()
    # content_descriptor = open("prompts/ppteval_describe_content.txt", "r").read()
    ppt_extractor = Template(open("prompts/ppteval_extract.txt", "r").read())
    logic_scorer = Template(open("ppteval_coherence.txt", "r").read())

    # for slide_image in glob(pjoin(slide_folder, "slide_*.jpg")):
    #     slide_descr = slide_image.replace(".jpg", ".json")
    #     if not os.path.exists(slide_descr):
    #         style_descr = llms.vision_model(style_descriptor, slide_image)
    #         content_descr = llms.vision_model(content_descriptor, slide_image)
    #         json.dump(
    #             {"content": content_descr, "style": style_descr},
    #             open(slide_descr, "w"),
    #             indent=4,
    #         )
    #     else:
    #         descr = json.load(open(slide_descr))
    #         style_descr = descr["style"]
    #         content_descr = descr["content"]
    #     if slide_image not in evals["vision"]:
    #         evals["vision"][slide_image] = llms.language_model(
    #             vision_scorer.render(descr=style_descr), return_json=True
    #         )
    #     if slide_image not in evals["content"]:
    #         evals["content"][slide_image] = llms.language_model(
    #             text_scorer.render(descr=content_descr), return_json=True
    #         )

    slide_descr = pjoin(os.path.dirname(presentation.source_file), "extracted.json")
    older_than(slide_descr, wait=True)
    if not pexists(slide_descr):
        extracted = llms.language_model(
            ppt_extractor.render(presentation=presentation.to_text()),
            return_json=True,
        )
        json.dump(extracted, open(slide_descr, "w"), indent=4)
    else:
        extracted = json.load(open(slide_descr))
    if presentation.source_file not in evals["logic"]:
        evals["logic"][presentation.source_file] = llms.language_model(
            logic_scorer.render(
                background_information=extracted.pop("metadata"),
                logical_structure=extracted,
            ),
            return_json=True,
        )
    json.dump(evals, open(eval_file, "w"), indent=4)


# ppt eval
def eval_experiment(
    setting: str,
    thread_num: int = 2,
    general_eval: bool = False,
    feature_eval: bool = False,
    ppt_eval: bool = False,
):
    assert setting != "*"
    llms.language_model, llms.vision_model, judge_name = judges[0]
    print(f"evaluating {setting} under {judge_name}")
    print(
        "eval config :",
        f"general_eval: {general_eval}, feature_eval: {feature_eval}, ppt_eval: {ppt_eval}",
    )
    eval_file = f"data/evals/{setting}_{judge_name}.json"
    eval_stats = defaultdict(dict)
    if pexists(eval_file):
        eval_stats |= json.load(open(eval_file))
    config = Config("/tmp")
    presentations = glob(f"data/*/pptx/*/{setting}/*/final.pptx")
    # filename dimension score
    print("start evaluation")
    if general_eval:
        eval_general(presentations, eval_stats)

    if feature_eval:
        eval_feature(presentations, eval_stats, setting)

    if ppt_eval:
        slide_image_folders = glob(f"data/*/pptx/*/final_images/{setting}/*")
        for presentation in presentations:
            eval_ppt(presentation)
        eval_stats = merge_evals(slide_image_folders, eval_stats)
    json.dump(eval_stats, open(eval_file, "w"), indent=4)


def dataset_stat():
    pdf_stat = {}
    ppt_stat = {}
    tempdir = TemporaryDirectory()
    config = Config()
    config.set_rundir(tempdir.name)
    for topic in topics:
        markdown_contents = {
            f: len(open(f, "r").read()) for f in glob(f"data/{topic}/pdf/*/*.md")
        }
        pdf_stat |= markdown_contents
        avg_pdf_text_len = sum(markdown_contents.values()) / len(markdown_contents)
        num_images = 0
        for pdf_folder in glob(f"data/{topic}/pdf/*"):
            images = json.load(open(pjoin(pdf_folder, "image_caption.json")))
            num_images += len(images)
        avg_pdf_images = num_images / len(markdown_contents)
        ppt_text_len = 0
        ppt_pages = 0
        ppt_images = 0
        num_ppts = 10
        for ppt_folder in glob(f"data/{topic}/pptx/*"):
            presentation = Presentation.from_file(
                pjoin(ppt_folder, "source.pptx"), config
            )
            ppt_stat[ppt_folder] = sum(
                [len(slide.to_text()) for slide in presentation.slides]
            )

            ppt_text_len += ppt_stat[ppt_folder]
            ppt_pages += len(presentation)
            ppt_images += len(os.listdir(pjoin(ppt_folder, "images")))

        avg_ppt_pages = ppt_pages / num_ppts
        avg_ppt_text_len = ppt_text_len / num_ppts
        avg_ppt_images = ppt_images / num_ppts
        print(
            "topic",
            "avg_pdf_text_len",
            "avg_pdf_images",
            "avg_ppt_pages",
            "avg_ppt_images",
            "avg_ppt_text_len",
        )
        print(
            f"{topic}: {avg_pdf_text_len:.2f}, {avg_pdf_images:.2f}, {avg_ppt_pages:.2f}, {avg_ppt_images:.2f}, {avg_ppt_text_len:.2f}"
        )

    json.dump(
        {"pdf": pdf_stat, "ppt": ppt_stat}, open("data/eval/stat.json", "w"), indent=4
    )


def pptx2images(settings: str = "*"):
    while True:
        for folder in glob(f"data/*/pptx/*/{settings}/*/history"):
            folder = os.path.dirname(folder)
            pptx = pjoin(folder, "final.pptx")
            ppt_folder, setting, pdf = folder.rsplit("/", 2)
            dst = pjoin(ppt_folder, "final_images", setting, pdf)

            if not pexists(pptx):
                if pexists(dst):
                    print(f"remove {dst}")
                    shutil.rmtree(dst)
                continue

            older_than(pptx)
            if pexists(dst):
                continue
            try:
                ppt_to_images(pptx, dst)
            except:
                print("pptx to images failed")
        sleep(60)
        print("keep scanning for new pptx")


def eval_baseline(setting: str):
    config = Config("/tmp")
    evals = defaultdict(dict)
    presentations = [
        Presentation.from_file(i, config)
        for i in glob(f"data/*/pdf/*/baseline_{setting}.pptx")
    ]
    eval_feature(presentations, evals, setting, fid_eval=False)
    json.dump(evals, open(f"data/evals/baseline_{setting}.json", "w"), indent=4)


if __name__ == "__main__":
    llms.vision_model = llms.gpt4o
    if len(sys.argv) > 1:
        func_argparse.main(
            dataset_stat,
            eval_experiment,
            pptx2images,
            eval_baseline,
            eval_ppt,
        )
    else:
        judge_idx = 1
        llms.language_model, llms.vision_model, judge_name = judges[judge_idx]
        eval_file = "test-logic.json"
        evals = defaultdict(dict)
        prs = glob("human_eval/*/*/final.pptx")
        with ThreadPoolExecutor(max_workers=24) as executor:
            executor.map(eval_ppt, prs, [os.path.dirname(i) for i in prs])
        evals = merge_evals([os.path.dirname(i) for i in prs], evals)
        json.dump(evals, open(eval_file, "w"), indent=4)
