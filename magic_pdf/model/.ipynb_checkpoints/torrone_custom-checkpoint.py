from loguru import logger
import os
import time
try:
    import cv2
    import yaml
    import argparse
    import numpy as np
    import torch

    from paddleocr import draw_ocr
    from PIL import Image
    from torchvision import transforms
    from torch.utils.data import Dataset, DataLoader
    from ultralytics import YOLO
    from unimernet.common.config import Config
    import unimernet.tasks as tasks
    from unimernet.processors import load_processor

    from magic_pdf.model.pek_sub_modules.layoutlmv3.model_init import Layoutlmv3_Predictor
    from magic_pdf.model.pek_sub_modules.post_process import get_croped_image, latex_rm_whitespace
    from magic_pdf.model.pek_sub_modules.self_modify import ModifiedPaddleOCR

    from torrone.postprocessing import markdown_compatible
    from torrone import TorroneModel
    from torrone.utils.device import move_to_device
    import re
    from huggingface_hub import login
    from torch.utils.data import ConcatDataset
    #albumentations 1.4.11
    #pip install timm==0.5.4 transformers==4.38.2

except ImportError as e:
    logger.exception(e)
    logger.error('Required dependency not installed, please install by \n"pip install magic-pdf[full-cpu] detectron2 --extra-index-url https://myhloli.github.io/wheels/"')
    exit(1)


def mfd_model_init(weight):
    mfd_model = YOLO(weight)
    return mfd_model


def mfr_model_init(weight_dir, cfg_path, _device_='cpu'):
    args = argparse.Namespace(cfg_path=cfg_path, options=None)
    cfg = Config(args)
    cfg.config.model.pretrained = os.path.join(weight_dir, "pytorch_model.bin")
    cfg.config.model.model_config.model_name = weight_dir
    cfg.config.model.tokenizer_config.path = weight_dir
    task = tasks.setup_task(cfg)
    model = task.build_model(cfg)
    model = model.to(_device_)
    vis_processor = load_processor('formula_image_eval', cfg.config.datasets.formula_rec_eval.vis_processor.eval)
    return model, vis_processor


def layout_model_init(weight, config_file, device):
    model = Layoutlmv3_Predictor(weight, config_file, device)
    return model


class MathDataset(Dataset):
    def __init__(self, image_paths, transform=None):
        self.image_paths = image_paths
        self.transform = transform

    def __len__(self):
        return len(self.image_paths)

    def __getitem__(self, idx):
        # if not pil image, then convert to pil image
        if isinstance(self.image_paths[idx], str):
            raw_image = Image.open(self.image_paths[idx])
        else:
            raw_image = self.image_paths[idx]
        if self.transform:
            image = self.transform(raw_image)
            return image


class CustomTorroneModel:

    def __init__(self, ocr: bool = False, show_log: bool = False, **kwargs):
        """
        ======== model init ========
        """
        # 获取当前文件（即 pdf_extract_kit.py）的绝对路径
        current_file_path = os.path.abspath(__file__)
        # 获取当前文件所在的目录(model)
        current_dir = os.path.dirname(current_file_path)
        # 上一级目录(magic_pdf)
        root_dir = os.path.dirname(current_dir)
        # model_config目录
        model_config_dir = os.path.join(root_dir, 'resources', 'model_config')
        # 构建 model_configs.yaml 文件的完整路径
        config_path = os.path.join(model_config_dir, 'model_configs.yaml')
        with open(config_path, "r") as f:
            self.configs = yaml.load(f, Loader=yaml.FullLoader)
        # 初始化解析配置
        self.apply_layout = kwargs.get("apply_layout", self.configs["config"]["layout"])
        self.apply_formula = kwargs.get("apply_formula", self.configs["config"]["formula"])
        self.apply_ocr = ocr
        logger.info(
            "DocAnalysis init, this may take some times. apply_layout: {}, apply_formula: {}, apply_ocr: {}".format(
                self.apply_layout, self.apply_formula, self.apply_ocr
            )
        )
        assert self.apply_layout, "DocAnalysis must contain layout model."
        # 初始化解析方案
        self.device = kwargs.get("device", self.configs["config"]["device"])
        logger.info("using device: {}".format(self.device))
        models_dir = kwargs.get("models_dir", os.path.join(root_dir, "resources", "models"))

        # 初始化公式识别
        if self.apply_formula:
            # 初始化公式检测模型
            self.mfd_model = mfd_model_init(str(os.path.join(models_dir, self.configs["weights"]["mfd"])))

            # 初始化公式解析模型
            mfr_weight_dir = str(os.path.join(models_dir, self.configs["weights"]["mfr"]))
            mfr_cfg_path = str(os.path.join(model_config_dir, "UniMERNet", "demo.yaml"))
            self.mfr_model, mfr_vis_processors = mfr_model_init(mfr_weight_dir, mfr_cfg_path, _device_=self.device)
            self.mfr_transform = transforms.Compose([mfr_vis_processors, ])

        # 初始化layout模型
        self.layout_model = Layoutlmv3_Predictor(
            str(os.path.join(models_dir, self.configs['weights']['layout'])),
            str(os.path.join(model_config_dir, "layoutlmv3", "layoutlmv3_base_inference.yaml")),
            device=self.device
        )
        # 初始化ocr
        if self.apply_ocr:
            self.ocr_model = ModifiedPaddleOCR(show_log=show_log,lang="pt") #MODIFIED still calling but could be removed with right modifications to code bellow

            login("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")
            model = TorroneModel.from_pretrained("/workspace/data_pipelines/0xCarbon/torrone2-base-10ep", ignore_mismatched_sizes=True)
            self.torrone_model = move_to_device(model, bf16=True, cuda=(self.device=="cuda"))
            self.torrone_model.eval()
            logger.info('TORRONE LOADED')

        logger.info('DocAnalysis init done!')

    def __call__(self, image):

        latex_filling_list = []
        mf_image_list = []

        # layout检测
        layout_start = time.time()
        layout_res = self.layout_model(image, ignore_catids=[])
        layout_cost = round(time.time() - layout_start, 2)
        logger.info(f"layout detection cost: {layout_cost}")

        # 公式检测
        mfd_res = self.mfd_model.predict(image, imgsz=1888, conf=0.25, iou=0.45, verbose=True)[0]
        for xyxy, conf, cla in zip(mfd_res.boxes.xyxy.cpu(), mfd_res.boxes.conf.cpu(), mfd_res.boxes.cls.cpu()):
            xmin, ymin, xmax, ymax = [int(p.item()) for p in xyxy]
            new_item = {
                'category_id': 13 + int(cla.item()),
                'poly': [xmin, ymin, xmax, ymin, xmax, ymax, xmin, ymax],
                'score': round(float(conf.item()), 2),
                'latex': '',
            }
            layout_res.append(new_item)
            latex_filling_list.append(new_item)
            bbox_img = get_croped_image(Image.fromarray(image), [xmin, ymin, xmax, ymax])
            mf_image_list.append(bbox_img)

        # 公式识别
        mfr_start = time.time()
        dataset = MathDataset(mf_image_list, transform=self.mfr_transform)
        dataloader = DataLoader(dataset, batch_size=64, num_workers=0)
        mfr_res = []
        for mf_img in dataloader:
            mf_img = mf_img.to(self.device)
            output = self.mfr_model.generate({'image': mf_img})
            mfr_res.extend(output['pred_str'])
        for res, latex in zip(latex_filling_list, mfr_res):
            res['latex'] = latex_rm_whitespace(latex)
        mfr_cost = round(time.time() - mfr_start, 2)
        logger.info(f"formula nums: {len(mf_image_list)}, mfr time: {mfr_cost}")

        # ocr识别
        if self.apply_ocr:
            ocr_start = time.time()
            pil_img = Image.fromarray(image)
            single_page_mfdetrec_res = []
            
            for res in layout_res:
                if int(res['category_id']) in [13, 14]:
                    xmin, ymin = int(res['poly'][0]), int(res['poly'][1])
                    xmax, ymax = int(res['poly'][4]), int(res['poly'][5])
                    single_page_mfdetrec_res.append({
                        "bbox": [xmin, ymin, xmax, ymax],
                    })
                    
            torrone_samples = []
            counter = 0
            box_list = []
            for res in layout_res: #could use torrone in batch
                if int(res['category_id']) in [0, 1, 2, 4, 6, 7]:  # 需要进行ocr的类别
                    xmin, ymin = int(res['poly'][0]), int(res['poly'][1])
                    xmax, ymax = int(res['poly'][4]), int(res['poly'][5])
                    crop_box = (xmin, ymin, xmax, ymax)
                    cropped_img = Image.new('RGB', pil_img.size, 'white')
                    cropped_img.paste(pil_img.crop(crop_box), crop_box)
                    cropped_img = cv2.cvtColor(np.asarray(cropped_img), cv2.COLOR_RGB2BGR)
                    ocr_res = self.ocr_model.ocr(cropped_img, mfd_res=single_page_mfdetrec_res,rec=False)[0]
                    # logger.info('First OCR res')
                    #output torrone
                    # logger.info(f"shape of image {image.shape}")
                    new_image = image#cv2.cvtColor(image, cv2.COLOR_RGB2RGBA)
                    # logger.info(f"shape of new_image {new_image.shape}")
                    mask = np.zeros(image.shape[:2], dtype="uint8")
                    cv2.rectangle(mask, (xmin, ymin), (xmax, ymax), 255, -1)
                    # pil_img_array  = np.asarray(pil_img)
                    # pil_img_array = cv2.cvtColor(pil_img_array, cv2.COLOR_RGB2RGBA)
                    masked_img = cv2.bitwise_and(new_image, new_image, mask=mask)
                    # masked_img = cv2.cvtColor(np.asarray(masked_img), cv2.COLOR_RGB2BGR)
                    # logger.info(f"shape of masked_img {masked_img.shape}")
                    
                    data = Image.fromarray(cropped_img) #test if i can pass the cropped or if I should mask the input
                    # logger.info('Image masked')
                    sample = self.torrone_model.encoder.prepare_input(data)
                    # logger.info(f"sample shape {sample.shape}")
                    counter+=1

                    if ocr_res:
                        torrone_samples.append(torch.utils.data.TensorDataset(sample[None,...]))
                        box_list.append(ocr_res[0])

                        

            dataloader = torch.utils.data.DataLoader(
                ConcatDataset(torrone_samples),
                batch_size=10,
                shuffle=False,
            )

            predictions = []
            for i, sample in enumerate((dataloader)):
                # print(sample)
                # logger.info(f"sample shape {sample[0].shape}")
                model_output = self.torrone_model.inference(
                    image_tensors=sample[0], early_stopping=False
                )
                predictions+=(model_output["predictions"])


            for j, box_ocr_res in enumerate(ocr_res):
                output = predictions[j]
                if output.strip() == "[MISSING_PAGE_POST]":
                    # uncaught repetitions -- most likely empty page
                    output_torrone = f"\n\n[MISSING_PAGE_EMPTY]\n\n"
                # elif skipping and model_output["repeats"][j] is not None:
                # elif False and model_output["repeats"][j] is not None:
                #     if model_output["repeats"][j] > 0:
                #         # If we end up here, it means the output is most likely not complete and was truncated.
                #         output_torrone = f"\n\n[MISSING_PAGE_FAIL]\n\n"
                #     else:
                #         # If we end up here, it means the document page is too different from the training domain.
                #         # This can happen e.g. for cover pages.
                #         output_torrone = f"\n\n[MISSING_PAGE_EMPTY]\n\n"
                else:
                    # if markdown:
                    if True:
                        output = markdown_compatible(output)
                    output_torrone = output

                
                output_torrone = re.sub(r"\n{3,}", "\n\n", output_torrone).strip()
                print(output_torrone)
                
                p1, p2, p3, p4 = box_ocr_res
                # text, score = box_ocr_res[1]
                layout_res.append({
                    'category_id': 15,
                    'poly': p1 + p2 + p3 + p4,
                    'score': round(1, 2),
                    'text': output_torrone,
                })
            
            ocr_cost = round(time.time() - ocr_start, 2)
            logger.info(f"ocr cost: {ocr_cost}")

        return layout_res