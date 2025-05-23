import argparse
import os
import shutil
import math
import torch.nn as nn
from importlib.resources import files

from cached_path import cached_path

from f5_tts.model import CFM, UNetT, DiT, Trainer, DurationPredictor
from f5_tts.model.utils import get_tokenizer
from f5_tts.model.dataset import load_dataset


# -------------------------- Dataset Settings --------------------------- #
target_sample_rate = 24000
n_mel_channels = 100
hop_length = 256
win_length = 1024
n_fft = 1024
mel_spec_type = "vocos"  # 'vocos' or 'bigvgan'


# -------------------------- Weight Initialization for TTS --------------------------- #
def init_weights_tts(model):
    """Initialize weights for TTS models using best practices.
    
    This follows TTS-specific best practices:
    - Transformer layers: Use Xavier/Glorot normal with gain=1.0
    - Linear layers: Xavier uniform with specific gain for projection layers
    - Embedding layers: Normal distribution with mean=0, std=0.02
    - LayerNorm: Initialize with bias=0, weight=1.0
    - Convolutional layers: Xavier uniform with calculated gain
    """
    print("Initializing model weights with TTS-specific best practices")
    
    for name, module in model.named_modules():
        # Initialize transformer attention weights
        if "attn" in name or "attention" in name:
            if isinstance(module, nn.Linear):
                # Query, key, value projections benefit from normal distribution
                if hasattr(module, 'weight') and module.weight is not None:
                    nn.init.xavier_normal_(module.weight, gain=1.0)
                if hasattr(module, 'bias') and module.bias is not None:
                    nn.init.zeros_(module.bias)
        
        # Initialize feed-forward networks
        elif "mlp" in name or "ffn" in name or "feed_forward" in name:
            if isinstance(module, nn.Linear):
                # FFN layers work well with uniform initialization
                if hasattr(module, 'weight') and module.weight is not None:
                    nn.init.xavier_uniform_(module.weight, gain=math.sqrt(2))
                if hasattr(module, 'bias') and module.bias is not None:
                    nn.init.zeros_(module.bias)
        
        # Initialize embedding layers
        elif isinstance(module, nn.Embedding):
            # Speech transformers often use this initialization for embeddings
            if hasattr(module, 'weight') and module.weight is not None:
                nn.init.normal_(module.weight, mean=0.0, std=0.02)
        
        # Initialize layer norm
        elif isinstance(module, nn.LayerNorm):
            if hasattr(module, 'weight') and module.weight is not None:
                nn.init.ones_(module.weight)
            if hasattr(module, 'bias') and module.bias is not None:
                nn.init.zeros_(module.bias)
        
        # Initialize all other linear layers
        elif isinstance(module, nn.Linear):
            if hasattr(module, 'weight') and module.weight is not None:
                # Output projection layers
                if "proj" in name or "projection" in name or "out" in name:
                    # Output projections benefit from smaller initialization
                    nn.init.xavier_uniform_(module.weight, gain=0.5)
                else:
                    nn.init.xavier_uniform_(module.weight)
            
            if hasattr(module, 'bias') and module.bias is not None:
                nn.init.zeros_(module.bias)
        
        # Initialize convolutional layers
        elif isinstance(module, nn.Conv1d) or isinstance(module, nn.Conv2d):
            if hasattr(module, 'weight') and module.weight is not None:
                # Calculate gain based on activation and kernel size
                fan_in = module.in_channels * module.kernel_size[0]
                if fan_in > 0:
                    std = 1.0 / math.sqrt(fan_in)
                    nn.init.uniform_(module.weight, -std, std)
                else:
                    nn.init.xavier_uniform_(module.weight)
            
            if hasattr(module, 'bias') and module.bias is not None:
                nn.init.zeros_(module.bias)

    return model

# -------------------------- Argument Parsing --------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(description="Train CFM Model")

    parser.add_argument(
        "--exp_name",
        type=str,
        default="F5TTS_v1_Base",
        choices=["F5TTS_v1_Base", "F5TTS_Base", "E2TTS_Base", "F5TTS_v1_Custom_Prune_14", "F5TTS_v1_Custom_Prune_12"],
        help="Experiment name",
    )
    parser.add_argument("--dataset_name", type=str, default="Emilia_ZH_EN", help="Name of the dataset to use")
    parser.add_argument("--learning_rate", type=float, default=1e-5, help="Learning rate for training")
    parser.add_argument("--weight_decay", type=float, default=0.001, help="Weight decay for optimizer")
    parser.add_argument("--batch_size_per_gpu", type=int, default=3200, help="Batch size per GPU")
    parser.add_argument(
        "--batch_size_type", type=str, default="frame", choices=["frame", "sample"], help="Batch size type"
    )
    parser.add_argument("--max_samples", type=int, default=64, help="Max sequences per batch")
    parser.add_argument("--grad_accumulation_steps", type=int, default=1, help="Gradient accumulation steps")
    parser.add_argument("--max_grad_norm", type=float, default=1.0, help="Max gradient norm for clipping")
    parser.add_argument("--epochs", type=int, default=100, help="Number of training epochs")
    parser.add_argument("--num_warmup_updates", type=int, default=20000, help="Warmup updates")
    parser.add_argument("--save_per_updates", type=int, default=50000, help="Save checkpoint every N updates")
    parser.add_argument(
        "--keep_last_n_checkpoints",
        type=int,
        default=-1,
        help="-1 to keep all, 0 to not save intermediate, > 0 to keep last N checkpoints",
    )
    parser.add_argument("--last_per_updates", type=int, default=5000, help="Save last checkpoint every N updates")
    parser.add_argument("--finetune", action="store_true", help="Use Finetune")
    parser.add_argument("--from_scratch", action="store_true", help="Train from scratch with initialized weights")
    parser.add_argument("--pretrain", type=str, default=None, help="the path to the checkpoint")
    parser.add_argument(
        "--tokenizer", type=str, default="pinyin", choices=["pinyin", "char", "custom"], help="Tokenizer type"
    )
    parser.add_argument(
        "--tokenizer_path",
        type=str,
        default=None,
        help="Path to custom tokenizer vocab file (only used if tokenizer = 'custom')",
    )
    parser.add_argument(
        "--log_samples",
        action="store_true",
        help="Log inferenced samples per ckpt save updates",
    )
    parser.add_argument("--logger", type=str, default=None, choices=[None, "wandb", "tensorboard"], help="logger")
    parser.add_argument(
        "--bnb_optimizer",
        action="store_true",
        help="Use 8-bit Adam optimizer from bitsandbytes",
    )
    parser.add_argument("--duration_loss_weight", type=float, default=0.1, 
                    help="Weight for the duration prediction loss (0.0 disables duration prediction)")
    parser.add_argument("--use_duration_predictor", action="store_true",
                    help="Enable the duration predictor")

    # Fix eval ref/text
    parser.add_argument(
        "--ref_audio_paths",
        type=str,
        nargs="+",
        default=None, 
        help="Paths to reference audio files for consistent sample generation"
    )
    parser.add_argument(
        "--ref_texts",
        type=str,
        nargs="+",
        default=None,
        help="Reference text descriptions for the audio files"
    )
    parser.add_argument(
        "--ref_sample_text_prompts",
        type=str,
        nargs="+",
        default=None,
        help="Text prompts to use when generating samples from reference audios"
    )

    return parser.parse_args()


# -------------------------- Training Settings -------------------------- #

def main():
    args = parse_args()
    
    # Check for from_scratch flag and update output message
    training_mode = "training from scratch" if args.from_scratch else "fine-tuning"
    print(f"Starting {training_mode} with args: {args}")

    # --- Define Base Paths ---
    try:
        base_data_path = str(files("f5_tts").joinpath("../../data"))
        base_ckpt_path = str(files("f5_tts").joinpath("../../ckpts"))
    except Exception as e:
        print(f"Error determining base paths relative to package: {e}")
        # Fallback assuming script is run from project root maybe? Adjust as needed.
        base_data_path = "./data"
        base_ckpt_path = "./ckpts"
        print(f"Warning: Using fallback paths '{base_data_path}' and '{base_ckpt_path}'. Ensure this is correct.")

    target_dataset_data_dir = os.path.join(base_data_path, args.dataset_name)
    target_dataset_ckpt_dir = os.path.join(base_ckpt_path, args.dataset_name)
    print(f"Target Dataset Data Dir: {target_dataset_data_dir}")
    print(f"Target Dataset Checkpoint Dir: {target_dataset_ckpt_dir}")


    # --- Determine Model Config and Initial Checkpoint Path (ckpt_path_source) ---
    ckpt_path_source = None # Path to the *source* checkpoint to load weights from
    model_cls = None
    model_cfg = None
    wandb_resume_id = None # Define here

    if args.exp_name == "F5TTS_v1_Custom_Prune_14":
        model_cls = DiT
        model_cfg = dict( # Model config MUST match the pruned model
            dim=1024,
            depth=14, # Correct depth for the pruned model
            heads=16, 
            ff_mult=2, 
            text_dim=512, 
            conv_layers=4,
            # Add other relevant base config keys if they exist in original and are needed
            # e.g., text_mask_padding=True, qk_norm=None, pe_attn_head=None might be needed based on original base config
        )
        # *** Source is the pruned+extended checkpoint CREATED by vocab_extend ***
        # Assumes it was saved/copied into the target ckpt dir with 'pretrained_' prefix
        ckpt_path_source = os.path.join(target_dataset_ckpt_dir, "pruned_baseV1.pt")

    elif args.exp_name == "F5TTS_v1_Custom_Prune_12":
        model_cls = DiT
        model_cfg = dict( # Model config MUST match the pruned model
            dim=1024,
            depth=12, # Correct depth for the pruned model
            heads=16, ff_mult=2, text_dim=512, conv_layers=4,
            # Add other relevant base config keys if they exist in original and are needed
            # e.g., text_mask_padding=True, qk_norm=None, pe_attn_head=None might be needed based on original base config
        )
        # *** Source is the pruned+extended checkpoint CREATED by vocab_extend ***
        # Assumes it was saved/copied into the target ckpt dir with 'pretrained_' prefix
        ckpt_path_source = os.path.join(target_dataset_ckpt_dir, "pruned_baseV1.pt")

    elif args.exp_name == "F5TTS_v1_Base":
         model_cls = DiT
         model_cfg = dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4) # Original config
         if args.finetune and not args.from_scratch:
             if args.pretrain is None: 
                 ckpt_path_source = str(cached_path("hf://SWivid/F5-TTS/F5TTS_v1_Base/model_1250000.safetensors"))
             else: 
                 ckpt_path_source = args.pretrain
             print(f"Selected {args.exp_name}. Source Checkpoint: {ckpt_path_source}")
         else:
             print(f"Selected {args.exp_name} architecture for training from scratch")

    # Add elif blocks for F5TTS_Base, E2TTS_Base here...

    else:
         raise ValueError(f"Unsupported experiment name: {args.exp_name}")

    # Modify the finetune check to also check for from_scratch
    # Only check checkpoint existence if not training from scratch
    if args.finetune and not args.from_scratch and (ckpt_path_source is None or not os.path.exists(ckpt_path_source)):
         # Check existence early if fine-tuning
         if ckpt_path_source is None:
             raise ValueError(f"Finetune selected but could not determine source checkpoint path for '{args.exp_name}'.")
         # Check if the determined source path exists *before* attempting copy
         # Exception: HF paths might not exist locally yet
         if not ckpt_path_source.startswith("hf://"):
             if not os.path.exists(ckpt_path_source):
                  raise FileNotFoundError(f"Determined source checkpoint path does not exist: {ckpt_path_source}")
         else:
              print("Source path is a Hugging Face identifier, existence will be checked by cached_path.")


    # --- Determine Correct Vocabulary File PATH for Model Initialization ---
    tokenizer_type_for_init = args.tokenizer # Start with user arg ('char' in your case)
    path_or_alias_for_get_tokenizer = args.dataset_name # Default: use dataset name alias

    if (args.exp_name == "F5TTS_v1_Custom_Prune_14" or args.exp_name == "F5TTS_v1_Custom_Prune_12"):
        # *** Use the SPECIFIC EXTENDED vocab.txt FILE ***
        # Path construction based on your dataset name and the tokenizer type you specified ('char')
        extended_vocab_file_path = os.path.join(base_data_path, f"{args.dataset_name}_{args.tokenizer}", "vocab.txt")
        print(f"Checking for extended vocab at: {extended_vocab_file_path}")
        if not os.path.isfile(extended_vocab_file_path):
             # Fallback: Try without the tokenizer suffix in the dir name? Sometimes structure varies.
             fallback_path = os.path.join(target_dataset_data_dir, "vocab.txt")
             print(f"Extended vocab not found at primary path, checking fallback: {fallback_path}")
             if os.path.isfile(fallback_path):
                  extended_vocab_file_path = fallback_path
             else:
                  raise FileNotFoundError(f"Extended vocabulary file not found at expected locations: "
                                     f"{os.path.join(base_data_path, f'{args.dataset_name}_{args.tokenizer}', 'vocab.txt')} or "
                                     f"{fallback_path}. Ensure vocab_extend saved vocab.txt correctly.")

        path_or_alias_for_get_tokenizer = extended_vocab_file_path # Pass the full FILE PATH
        tokenizer_type_for_init = "custom" # Signal get_tokenizer to use the provided path directly
        print(f"Using EXTENDED vocab FILE for model init: {path_or_alias_for_get_tokenizer}")

    elif args.tokenizer == "custom":
        if not args.tokenizer_path or not os.path.isfile(args.tokenizer_path):
             raise ValueError(f"Custom tokenizer selected, but --tokenizer_path '{args.tokenizer_path}' is invalid or not found.")
        path_or_alias_for_get_tokenizer = args.tokenizer_path
        tokenizer_type_for_init = "custom"
        print(f"Using custom tokenizer file from args: {path_or_alias_for_get_tokenizer}")
    else:
        # Default behavior uses dataset name alias and args.tokenizer type
        path_or_alias_for_get_tokenizer = args.dataset_name
        tokenizer_type_for_init = args.tokenizer
        print(f"Using default tokenizer associated with dataset '{args.dataset_name}' and type '{tokenizer_type_for_init}'")


    # --- Get Tokenizer Map and Correct Vocab Size ---
    print(f"Calling get_tokenizer with source='{path_or_alias_for_get_tokenizer}', type='{tokenizer_type_for_init}'")
    vocab_char_map, vocab_size = get_tokenizer(path_or_alias_for_get_tokenizer, tokenizer_type_for_init)
    print(f"\nDetermined Vocab Size for Model Init: {vocab_size}") # CRITICAL: Must match checkpoint


    # --- Define mel_spec_kwargs HERE ---
    mel_spec_kwargs = dict(
        n_fft=n_fft,
        hop_length=hop_length,
        win_length=win_length,
        n_mel_channels=n_mel_channels,
        target_sample_rate=target_sample_rate,
        mel_spec_type=mel_spec_type,
    )


    # --- Model Instantiation ---
    if model_cls is None or model_cfg is None: raise RuntimeError("Model class or config was not set correctly.")
    print(f"Initializing {model_cls.__name__} with config: {model_cfg}, Vocab Size: {vocab_size}")
    # The vocab_size used here MUST match the embedding layer in ckpt_path_source
    model = CFM(
        transformer=model_cls(**model_cfg, text_num_embeds=vocab_size, mel_dim=n_mel_channels),
        mel_spec_kwargs=mel_spec_kwargs, # Now defined
        vocab_char_map=vocab_char_map,
    )
    
    # Initialize duration predictor if enabled
    duration_predictor = None
    if args.use_duration_predictor and args.duration_loss_weight > 0:
        print("Initializing Duration Predictor")
        duration_predictor = DurationPredictor(vocab_size, 512, 32, 3, 0.5)
        
        # Apply TTS weight initialization to duration predictor if training from scratch
        if args.from_scratch:
            print("Applying TTS-specific weight initialization to Duration Predictor")
            init_weights_tts(duration_predictor)
    elif args.use_duration_predictor:
        print("Duration predictor requested but duration_loss_weight is 0. Setting up duration predictor anyway.")
        duration_predictor = DurationPredictor(vocab_size, 512, 32, 3, 0.5)
        if args.from_scratch:
            init_weights_tts(duration_predictor)
    else:
        print("Duration predictor disabled")
    
    # Apply custom TTS weight initialization if training from scratch
    if args.from_scratch:
        print("Applying TTS-specific weight initialization for training from scratch")
        model = init_weights_tts(model)
        # Recommend higher learning rate for from-scratch training
        if args.learning_rate <= 1e-5:
            print(f"Note: Consider using a higher learning rate (e.g., 1e-4) for training from scratch.")
            print(f"Current learning rate: {args.learning_rate}")
    
    print("Model Initialized.")


    # --- Copy Checkpoint for Trainer (Only if fine-tuning and not from scratch) ---
    # The Trainer will load from target_dataset_ckpt_dir
    if args.finetune and not args.from_scratch:
        if not os.path.isdir(target_dataset_ckpt_dir):
             print(f"Creating checkpoint directory: {target_dataset_ckpt_dir}")
             os.makedirs(target_dataset_ckpt_dir, exist_ok=True)

        # Determine the target filename within the checkpoint directory
        source_ckpt_basename = os.path.basename(ckpt_path_source)
        target_basename = "pretrained_" + source_ckpt_basename if not source_ckpt_basename.startswith("pretrained_") else source_ckpt_basename
        file_checkpoint_for_trainer = os.path.join(target_dataset_ckpt_dir, target_basename)

        # Copy only if source exists and target doesn't
        if not os.path.isfile(file_checkpoint_for_trainer):
             if not os.path.exists(ckpt_path_source):
                 # Re-check after potential HF download via cached_path if applicable
                 if ckpt_path_source.startswith("hf://"):
                      try:
                           # Trigger download if needed, will raise error if invalid
                           cached_path(ckpt_path_source)
                           # If download worked, path might resolve differently? Usually cached_path handles it.
                           # Let's assume the original ckpt_path_source is valid if no exception.
                           print(f"Checked Hugging Face path: {ckpt_path_source}")
                      except Exception as e:
                           raise FileNotFoundError(f"Source checkpoint '{ckpt_path_source}' not found locally and failed to resolve/download: {e}")
                 else: # Local path check failed earlier
                      raise FileNotFoundError(f"Source checkpoint for copying not found: {ckpt_path_source}")

             print(f"Copying checkpoint for finetune from '{ckpt_path_source}' to '{file_checkpoint_for_trainer}'")
             try:
                 shutil.copy2(ckpt_path_source, file_checkpoint_for_trainer)
             except Exception as e:
                 print(f"Error during checkpoint copy: {e}")
                 raise
        else:
             print(f"Pretrained checkpoint already exists: {file_checkpoint_for_trainer}. Using existing.")
    elif args.from_scratch:
        # Ensure checkpoint directory exists for saving during training from scratch
        if not os.path.isdir(target_dataset_ckpt_dir):
             print(f"Creating checkpoint directory for training from scratch: {target_dataset_ckpt_dir}")
             os.makedirs(target_dataset_ckpt_dir, exist_ok=True)


    # --- Trainer Initialization ---
    print(f"Initializing Trainer. Checkpoint directory: {target_dataset_ckpt_dir}")
    trainer = Trainer(
        model,
        args.epochs,
        args.learning_rate,
        args.weight_decay,
        checkpoint_path=target_dataset_ckpt_dir,
        num_warmup_updates=args.num_warmup_updates,
        save_per_updates=args.save_per_updates,
        keep_last_n_checkpoints=args.keep_last_n_checkpoints,
        batch_size_per_gpu=args.batch_size_per_gpu,
        batch_size_type=args.batch_size_type,
        max_samples=args.max_samples,
        grad_accumulation_steps=args.grad_accumulation_steps,
        max_grad_norm=args.max_grad_norm,
        logger=args.logger,
        wandb_project=args.dataset_name,
        wandb_run_name=f"{args.exp_name}_{'scratch' if args.from_scratch else 'finetune'}_{args.dataset_name}",
        wandb_resume_id=wandb_resume_id,
        log_samples=args.log_samples,
        last_per_updates=args.last_per_updates,
        bnb_optimizer=args.bnb_optimizer,
        mel_spec_type=mel_spec_type,
        # Add the duration predictor and its weight
        duration_predictor=duration_predictor,
        duration_loss_weight=args.duration_loss_weight,
        # Add the new parameters
        ref_texts=args.ref_texts,
        ref_audio_paths=args.ref_audio_paths,
        ref_sample_text_prompts=args.ref_sample_text_prompts,
    )
    print("Trainer Initialized.")


    # --- Load Dataset ---
    print(f"Loading dataset '{args.dataset_name}' using tokenizer type '{args.tokenizer}'")
    train_dataset = load_dataset(args.dataset_name, args.tokenizer, mel_spec_kwargs=mel_spec_kwargs)
    print("Dataset Loaded.")


    # --- Start Training ---
    print("Starting trainer.train()...")
    trainer.train(
        train_dataset,
        resumable_with_seed=666,
    )
    print("Training finished or exited.")


if __name__ == "__main__":
    main()