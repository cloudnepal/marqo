import json
import os
import time
from threading import Lock

import nltk
import torch
from PIL import Image

from marqo import config, version
from marqo import marqo_docs
from marqo.api import exceptions
from marqo.connections import redis_driver
from marqo.s2_inference.constants import PATCH_MODELS
from marqo.s2_inference.processing.image import chunk_image
from marqo.s2_inference.s2_inference import vectorise
# we need to import backend before index_meta_cache to prevent circular import error:
from marqo.tensor_search import constants
from marqo.tensor_search import index_meta_cache, utils
from marqo.tensor_search.enums import EnvVars
from marqo.tensor_search.tensor_search_logging import get_logger
from marqo import marqo_docs
import subprocess
import nltk



logger = get_logger(__name__)


def on_start(config: config.Config):
    to_run_on_start = (
        BootstrapVespa(config),
        PopulateCache(config),
        DownloadStartText(),
        CUDAAvailable(),
        SetBestAvailableDevice(),
        SetEnableVideoGPUAcceleration(),
        CheckNLTKTokenizers(),
        CacheModels(),
        InitializeRedis("localhost", 6379),
        CachePatchModels(),
        DownloadFinishText(),
        PrintVersion(),
        MarqoWelcome(),
        MarqoPhrase(),
    )

    for thing_to_start in to_run_on_start:
        thing_to_start.run()


class BootstrapVespa:
    """Create the Marqo settings schema on Vespa"""

    def __init__(self, config: config.Config):
        self.config = config

    def run(self):
        try:
            logger.debug('Bootstrapping Vespa')
            created = self.config.index_management.bootstrap_vespa()
            if created:
                logger.debug('Vespa configured successfully')
            else:
                logger.debug('Vespa configuration already exists. Skipping bootstrap')
        except Exception as e:
            logger.error(
                f"Failed to bootstrap vector store. If you are using an external vector store, "
                "ensure that Marqo is configured properly for this. See "
                f"{marqo_docs.configuring_marqo()} for more details. Error: {e}"
            )
            raise e


class PopulateCache:
    """Populates the cache on start"""

    def __init__(self, config: config.Config):
        self.config = config

    def run(self):
        logger.debug('Starting index cache refresh thread')
        index_meta_cache.start_refresh_thread(self.config)


class CUDAAvailable:
    # TODO [Refactoring device logic] move this logic to device manager
    """checks the status of cuda
    """
    logger = get_logger('CUDA device summary')

    def run(self):
        def id_to_device(id):
            if id < 0:
                return ['cpu']
            return [torch.cuda.get_device_name(id)]

        device_count = 0 if not torch.cuda.is_available() else torch.cuda.device_count()

        # use -1 for cpu
        device_ids = [-1]
        device_ids += list(range(device_count))

        device_names = []
        for device_id in device_ids:
            device_names.append({'id': device_id, 'name': id_to_device(device_id)})

        self.logger.info(f"Found devices {device_names}")


class SetBestAvailableDevice:
    # TODO [Refactoring device logic] move this logic to device manager, get rid of MARQO_BEST_AVAILABLE_DEVICE envvar
    """sets the MARQO_BEST_AVAILABLE_DEVICE env var
    """
    logger = get_logger('SetBestAvailableDevice')

    def run(self):
        """
            This is set once at startup time. We assume it will NOT change,
            if it does, health check should throw a warning.
        """
        if torch.cuda.is_available():
            os.environ[EnvVars.MARQO_BEST_AVAILABLE_DEVICE] = "cuda"
        else:
            os.environ[EnvVars.MARQO_BEST_AVAILABLE_DEVICE] = "cpu"

        self.logger.info(f"Best available device set to: {os.environ[EnvVars.MARQO_BEST_AVAILABLE_DEVICE]}")


class CacheModels:
    """warms the in-memory model cache by preloading good defaults
    """
    logger = get_logger('ModelsForStartup')

    def __init__(self):
        warmed_models = utils.read_env_vars_and_defaults(EnvVars.MARQO_MODELS_TO_PRELOAD)
        if warmed_models is None:
            self.models = []
        elif isinstance(warmed_models, str):
            try:
                self.models = json.loads(warmed_models)
            except json.JSONDecodeError as e:
                # TODO: Change error message to match new format
                raise exceptions.EnvVarError(
                    f"Could not parse environment variable `{EnvVars.MARQO_MODELS_TO_PRELOAD}`. "
                    f"Please ensure that this a JSON-encoded array of strings or dicts. For example:\n"
                    f"""export {EnvVars.MARQO_MODELS_TO_PRELOAD}='["ViT-L/14", "onnx/all_datasets_v4_MiniLM-L6"]'"""
                    f"To add a custom model, it must be a dict with keys `model` and `model_properties` " 
                    f"as defined in {marqo_docs.bring_your_own_model()}"
                ) from e
        else:
            self.models = warmed_models
        # TBD to include cross-encoder/ms-marco-TinyBERT-L-2-v2

        # TODO [Refactoring device logic] use device info gathered from device manager
        self.default_devices = ['cpu'] if not torch.cuda.is_available() else ['cuda', 'cpu']

        self.logger.info(f"pre-loading {self.models} onto devices={self.default_devices}")

    def run(self):
        test_string = 'this is a test string'
        N = 10
        messages = []
        for model in self.models:
            # Skip preloading of models that can't be preloaded (eg. no_model)
            if isinstance(model, str):
                model_name = model
            elif isinstance(model, dict):
                try:
                    model_name = model["model"]
                except KeyError as e:
                    raise exceptions.EnvVarError(
                        f"Your custom model {model} is missing 'model' key."
                        f"To add a custom model, it must be a dict with keys 'model' and 'model_properties' "
                        f"as defined in '{marqo_docs.configuring_preloaded_models()}'"
                    ) from e
            else:
                continue

            if model_name in constants.MODELS_TO_SKIP_PRELOADING:
                self.logger.info(f"Skipping preloading of '{model_name}' because the model does not require preloading.")
                continue
            for device in self.default_devices:
                self.logger.debug(f"Loading model: {model} on device: {device}")

                # warm it up
                _ = _preload_model(model=model, content=test_string, device=device)

                t = 0
                for n in range(N):
                    t0 = time.time()
                    _ = _preload_model(model=model, content=test_string, device=device)
                    t1 = time.time()
                    t += (t1 - t0)
                message = f"{(t) / float((N))} for {model} and {device}"
                messages.append(message)
                self.logger.debug(f"{model} {device} vectorise run {N} times.")
                self.logger.info(f"{model} {device} run succesfully!")

        for message in messages:
            self.logger.info(message)
        self.logger.info("completed loading models")

class CachePatchModels:
    """Prewarm patch models"""

    logger = get_logger('CachePatchModels')
    lock = Lock()

    def __init__(self):
        warmed_models = utils.read_env_vars_and_defaults(EnvVars.MARQO_PATCH_MODELS_TO_PRELOAD)
        if warmed_models is None:
            self.models = []
        elif isinstance(warmed_models, str):
            try:
                self.models = json.loads(warmed_models)
            except json.JSONDecodeError as e:
                raise exceptions.EnvVarError(
                    f"Could not parse environment variable `{EnvVars.MARQO_PATCH_MODELS_TO_PRELOAD}`. "
                    f"Please ensure that this is a JSON-encoded list of strings."
                ) from e
        elif isinstance(warmed_models, list):
            self.models = warmed_models
        else:
            raise exceptions.EnvVarError(
                f"Environment variable `{EnvVars.MARQO_PATCH_MODELS_TO_PRELOAD}` should be a list of strings."
            )
        
        for model in self.models:
            if model not in PATCH_MODELS:
                raise exceptions.EnvVarError(
                    f"Invalid patch model: {model}. Please ensure that this is a valid patch model."
                )

        # TODO [Refactoring device logic] use device info gathered from device manager
        self.default_devices = ['cpu'] if not torch.cuda.is_available() else ['cpu', 'cuda']

    def run(self):
        N = 10
        messages = []
        test_image = torch.zeros((3, 224, 224))  # Dummy image tensor
        test_image_pil = Image.fromarray(test_image.numpy().astype('uint8').transpose(1, 2, 0))  # Convert to PIL image
        for model in self.models:
            for device in self.default_devices:
                self.logger.debug(f"Prewarming model: {model} on device: {device}")
                with self.lock:
                    try:
                        # Warm it up
                        chunks = chunk_image(test_image_pil, device=device, method=model)

                        t = 0
                        for n in range(N):
                            t0 = time.time()
                            chunks = chunk_image(test_image_pil, device=device, method=model)
                            t1 = time.time()
                            t += (t1 - t0)
                        message = f"{(t) / float((N))} for {model} and {device}"
                        messages.append(message)
                        self.logger.debug(f"{model} {device} ran chunking {N} times.")
                        self.logger.info(f"{model} {device} chunking run succesfully!")

                    except Exception as e:
                        self.logger.error(f"Failed to prewarm model: {model} on device: {device}. Error: {e}")

                self.logger.info(f"Prewarmed model: {model} on device: {device}")
            
        for message in messages:
            self.logger.info(message)
        self.logger.info("completed prewarming patch models")

class SetEnableVideoGPUAcceleration:

    logger = get_logger('SetVideoProcessingDevice')

    def run(self):
        """This method will set the env var MARQO_ENABLE_VIDEO_GPU_ACCELERATION to TRUE or FALSE."""
        env_value = utils.read_env_vars_and_defaults(EnvVars.MARQO_ENABLE_VIDEO_GPU_ACCELERATION)
        if env_value is None:
            try:
                self._check_video_gpu_acceleration_availability()
                os.environ[EnvVars.MARQO_ENABLE_VIDEO_GPU_ACCELERATION] = "TRUE"
            except exceptions.StartupSanityCheckError as e:
                self.logger.debug(f"Failed to use GPU acceleration for video processing. We will disable it. "
                                  f"Original error message: {e}")
                os.environ[EnvVars.MARQO_ENABLE_VIDEO_GPU_ACCELERATION] = "FALSE"
        elif env_value == "TRUE":
            self._check_video_gpu_acceleration_availability()
        elif env_value == "FALSE":
            pass
        else:
            raise exceptions.EnvVarError(
                f"Invalid value for {EnvVars.MARQO_ENABLE_VIDEO_GPU_ACCELERATION}. "
                f"Please set it to either 'TRUE' or 'FALSE'."
            )

    def _check_video_gpu_acceleration_availability(self):
        """Check if the required dependencies are available for video processing with GPU acceleration for ffmpeg.

        Raises:
            exceptions.StartupSanityCheckError: If the required dependencies are not available.
        """
        ffmpeg_command_gpu_check = [
            'ffmpeg',
            '-v', 'error',  # Suppress output
            '-hwaccel', 'cuda',  # Use CUDA for hardware acceleration
            '-f', 'lavfi',  # Input format is a lavfi (FFmpeg's built-in filter)
            '-i', 'nullsrc=s=200x100',  # Generate a blank video source of 200x100 resolution
            '-vframes', '1',  # Process only 1 frame
            '-c:v', 'h264_nvenc',  # Use NVENC encoder
            '-f', 'null',  # Output to null (discard the output)
            '-'  # Output to stdout (discarded)
        ]
        try:
            _ = subprocess.run(
                ffmpeg_command_gpu_check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                text=True, timeout=10
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise exceptions.StartupSanityCheckError(
                f"Failed to use GPU acceleration for video processing. "
                f"Ensure that your system has the required dependencies installed. "
                f"You can set 'MARQO_ENABLE_VIDEO_GPU_ACCELERATION=FALSE' to disable GPU acceleration. "
                f"Check {marqo_docs.configuring_marqo()} for more information. "
                f"Original error message: {e.stderr}"
            ) from e
        except (ValueError, OSError) as e:
            raise exceptions.StartupSanityCheckError(
                f"Marqo failed to run the ffmpeg sanity check. Your ffmepeg installation might be broken. "
                f"Original error: {e}"
            ) from e


class CheckNLTKTokenizers:
    """Check if NLTK tokenizers are available, if not, download them.

    NLTK tokenizers are included in the base-image, we do a sanity check to ensure they are available.
    """
    def run(self):
        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError:
            logger.info("NLTK punkt_tab tokenizer not found. Downloading...")
            nltk.download("punkt_tab")

        try:
            nltk.data.find("tokenizers/punkt_tab")
        except LookupError as e:
            raise exceptions.StartupSanityCheckError(
                f"Marqo failed to download and download NLTK tokenizers. Original error: {e}"
            ) from e


def _preload_model(model, content, device):
    """
        Calls vectorise for a model once. This will load in the model if it isn't already loaded.
        If `model` is a str, it should be a model name in the registry
        If `model is a dict, it should be an object containing `model_name` and `model_properties`
        Model properties will be passed to vectorise call if object exists
    """
    if isinstance(model, str):
        # For models IN REGISTRY
        _ = vectorise(
            model_name=model,
            content=content,
            device=device
        )
    elif isinstance(model, dict):
        # For models from URL
        """
        TODO: include validation from on start script (model name properties etc)
        _check_model_name(index_settings)
        """
        try:
            _ = vectorise(
                model_name=model["model"],
                model_properties=model["modelProperties"],
                content=content,
                device=device
            )
        except KeyError as e:
            raise exceptions.EnvVarError(
                f"Your custom model {model} is missing either `model` or `model_properties`."
                f"To add a custom model, it must be a dict with keys `model` and `model_properties`. "
                f"See the examples defined in {marqo_docs.configuring_preloaded_models()}"
            ) from e


class InitializeRedis:

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port

    def run(self):
        logger.debug('Initializing Redis')
        # Can be turned off with MARQO_ENABLE_THROTTLING = 'FALSE'
        if utils.read_env_vars_and_defaults(EnvVars.MARQO_ENABLE_THROTTLING) == "TRUE":
            redis_driver.init_from_app(self.host, self.port)


class DownloadStartText:

    def run(self):
        print('\n')
        print("###########################################################")
        print("###########################################################")
        print("###### STARTING DOWNLOAD OF MARQO ARTEFACTS################")
        print("###########################################################")
        print("###########################################################")
        print('\n', flush=True)


class DownloadFinishText:

    def run(self):
        print('\n')
        print("###########################################################")
        print("###########################################################")
        print("###### !!COMPLETED SUCCESSFULLY!!!         ################")
        print("###########################################################")
        print("###########################################################")
        print('\n', flush=True)


class PrintVersion:
    def run(self):
        print(f"Version: {version.__version__}")


class MarqoPhrase:

    def run(self):
        message = r"""
     _____                                                   _        __              _                                     
    |_   _|__ _ __  ___  ___  _ __   ___  ___  __ _ _ __ ___| |__    / _| ___  _ __  | |__  _   _ _ __ ___   __ _ _ __  ___ 
      | |/ _ \ '_ \/ __|/ _ \| '__| / __|/ _ \/ _` | '__/ __| '_ \  | |_ / _ \| '__| | '_ \| | | | '_ ` _ \ / _` | '_ \/ __|
      | |  __/ | | \__ \ (_) | |    \__ \  __/ (_| | | | (__| | | | |  _| (_) | |    | | | | |_| | | | | | | (_| | | | \__ \
      |_|\___|_| |_|___/\___/|_|    |___/\___|\__,_|_|  \___|_| |_| |_|  \___/|_|    |_| |_|\__,_|_| |_| |_|\__,_|_| |_|___/
                                                                                                                                                                                                                                                     
        """

        print(message, flush=True)


class MarqoWelcome:

    def run(self):
        message = r"""   
     __    __    ___  _        __   ___   ___ ___    ___      ______   ___       ___ ___   ____  ____   ___    ___   __ 
    |  |__|  |  /  _]| |      /  ] /   \ |   |   |  /  _]    |      | /   \     |   |   | /    ||    \ /   \  /   \ |  |
    |  |  |  | /  [_ | |     /  / |     || _   _ | /  [_     |      ||     |    | _   _ ||  o  ||  D  )     ||     ||  |
    |  |  |  ||    _]| |___ /  /  |  O  ||  \_/  ||    _]    |_|  |_||  O  |    |  \_/  ||     ||    /|  Q  ||  O  ||__|
    |  `  '  ||   [_ |     /   \_ |     ||   |   ||   [_       |  |  |     |    |   |   ||  _  ||    \|     ||     | __ 
     \      / |     ||     \     ||     ||   |   ||     |      |  |  |     |    |   |   ||  |  ||  .  \     ||     ||  |
      \_/\_/  |_____||_____|\____| \___/ |___|___||_____|      |__|   \___/     |___|___||__|__||__|\_|\__,_| \___/ |__|
                                                                                                                        
        """
        print(message, flush=True)
