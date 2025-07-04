# src/trainer.py

import copy
import time
import os

import torch
from torch import nn, optim
from torch.optim.lr_scheduler import StepLR
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from ultralytics import YOLO


class Trainer:
    """
    Trainer class for managing model training, validation, and logging.

    Attributes:
        model (nn.Module): Model to be trained.
        device (torch.device): Device used (CPU or CUDA).
        train_loader (DataLoader): Training data.
        valid_loader (DataLoader): Validation data.
        classes (list): List of class names.
        tensorboard_dir (str): Directory for TensorBoard logs.
        model_file (str): Path to save the trained model.
        criterion (Loss): Loss function used (CrossEntropyLoss).
        optimizer (Optimizer): SGD or Adam optimizer.
        scheduler (LR Scheduler): StepLR scheduler.
        writer (SummaryWriter): TensorBoard writer.
    """

    def __init__(self, model, data, device, optimizer,
                 learning_rate, tensorboard_dir,
                 model_file, criterion):
        self.model = model.to(device)
        self.device = device
        self.train_loader = data["train"]
        self.valid_loader = data["valid"]
        self.classes = data["classes"]
        self.tensorboard_dir = tensorboard_dir
        self.model_file = model_file

        self.writer = SummaryWriter(log_dir=self.tensorboard_dir)
        self.criterion = criterion

        params = filter(lambda p: p.requires_grad, self.model.parameters())

        if optimizer == "Adam":
            self.optimizer = optim.Adam(params, lr=learning_rate)
        elif optimizer == "SGD":
            self.optimizer = optim.SGD(params, lr=learning_rate, momentum=0.9)
        else:
            raise ValueError(f"Unsupported optimizer: {optimizer}")

        self.scheduler = StepLR(self.optimizer, step_size=20, gamma=0.5)

    def _log_layers(self, epoch: int) -> None:
        """
        Logs the weights of linear layers for analysis in TensorBoard.
        """
        for i, layer in enumerate(self.model.modules()):
            if isinstance(layer, nn.Linear):
                self.writer.add_histogram(
                    tag=f"Weight/layer{i}",
                    values=layer.weight,
                    global_step=epoch
                )

    def validate(self) -> tuple:
        """
        Evaluates the model on the validation set.

        Returns:
            tuple: Average validation loss and accuracy.
        """
        self.model.eval()
        total, correct, val_loss = 0, 0, 0.0

        with torch.no_grad():
            for x, labels in self.valid_loader:
                x, labels = x.to(self.device), labels.to(self.device)
                outputs = self.model(x)
                loss = self.criterion(outputs, labels)
                preds = outputs.argmax(dim=1)

                val_loss += loss.item() * x.size(0)
                correct += (preds == labels).sum().item()
                total += labels.size(0)

        avg_loss = val_loss / total
        accuracy = 100.0 * correct / total
        return avg_loss, accuracy

    def train(
        self,
        epochs: int,
        log_layers: bool = False,
        save_best: bool = True,
        upper_bound: float = 100.0,
        debug: bool = False
    ) -> nn.Module:
        """
        Main training loop for the model.

        Args:
            epochs (int): Number of training epochs.
            log_layers (bool): Whether to log layer histograms.
            save_best (bool): Whether to save the best performing model.
            upper_bound (float): Early stopping threshold.
            debug (bool): If True, prints additional debug information.

        Returns:
            nn.Module: The best performing model.
        """
        start_time = time.time()
        best_acc = 0.0
        best_model = None
        steps_per_epoch = len(self.train_loader)

        for epoch in tqdm(range(epochs), desc="Training"):
            self.model.train()
            if debug:
                epoch_start = time.time()

            for i, (x, y) in enumerate(self.train_loader):
                x, y = x.to(self.device), y.to(self.device)

                outputs = self.model(x)
                loss = self.criterion(outputs, y)

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()

                step = epoch * steps_per_epoch + i
                acc = (outputs.argmax(1) == y).float().mean().item()

                self.writer.add_scalar("Loss/train", loss.item(), step)
                self.writer.add_scalar("Accuracy/train", acc, step)

            if log_layers:
                self._log_layers(epoch)

            val_loss, val_acc = self.validate()
            self.writer.add_scalar("Loss/val", val_loss, epoch)
            self.writer.add_scalar("Accuracy/val", val_acc, epoch)
            self.scheduler.step()

            if val_acc > best_acc:
                best_acc = val_acc
                best_model = copy.deepcopy(self.model)
                print(
                    f"\nNew best model found at epoch {epoch+1} "
                    f"(accuracy: {val_acc:.2f}%)\n"
                )

            if val_acc >= upper_bound:
                print(
                    f"Early stopping at epoch {epoch+1} "
                    f"(accuracy: {val_acc:.2f}%)"
                )
                break

            if debug:
                epoch_duration = time.time() - epoch_start
                print(
                    f"Epoch {epoch+1}/{epochs} completed "
                    f"in {epoch_duration:.2f}s"
                    f" (Loss: {val_loss:.4f}, "
                    f"Accuracy: {val_acc:.2f}%)"
                )

        # Report and log total training time
        elapsed = time.time() - start_time
        minutes, seconds = divmod(elapsed, 60)
        print(f"⏱️ Total training time: {int(minutes)}m {int(seconds)}s")

        time_log_path = f"{self.tensorboard_dir}_training_time.txt"
        with open(time_log_path, "w") as f:
            f.write(
                f"{elapsed:.2f} seconds ({int(minutes)}m {int(seconds)}s)\n"
            )

        # Save model if required
        if save_best and best_model:
            os.makedirs(os.path.dirname(self.model_file), exist_ok=True)
            save_path = f"{self.model_file}-{best_acc:.2f}.pkl"
            torch.save(best_model.state_dict(), save_path)

        self.writer.close()
        return best_model


class YOLOTrainer:
    """
    YOLOTrainer is a utility class for configuring, training, and saving YOLO
    models.

    Attributes:
        model_path (str): Path to the YOLO model weights or configuration file.
        dataset_yaml (str): Path to the dataset YAML file describing training
            and validation data.
        output_dir (str): Directory where training outputs and logs will be
            saved.
        experiment_name (str): Name of the experiment for organizing results.
        batch_size (int): Number of samples per training batch (default: 32).
        epochs (int): Number of training epochs (default: 10).
        img_size (int): Input image size for training (default: 224).
        freeze (int): Number of layers to freeze during training (default: 2).
        optimizer (str): Optimizer to use for training (default: "SGD").
        lr (float): Initial learning rate (default: 1e-3).
        device (str): Device to use for training, e.g., "cpu" or "cuda"
            (default: "cpu").

    Methods:
        train():
            Loads the YOLO model and trains it using the specified dataset and
                hyperparameters.
            Returns the trained model and training results.

        save_model(model: YOLO, save_path: str) -> None:
            Saves the provided YOLO model to the specified file path, creating
                directories as needed.
    """

    def __init__(
        self,
        model_path: str,
        dataset_yaml: str,
        output_dir: str,
        experiment_name: str,
        batch_size: int = 32,
        epochs: int = 10,
        img_size: int = 224,
        freeze: int = 2,
        optimizer: str = "SGD",
        lr: float = 1e-3,
        device: str = "cpu"
    ):
        """
        Initializes the trainer with the specified configuration.

        Args:
            model_path (str): Path to the model file or architecture.
            dataset_yaml (str): Path to the dataset configuration YAML file.
            output_dir (str): Directory where outputs (logs, checkpoints) will
                be saved.
            experiment_name (str): Name of the experiment for tracking and
                logging.
            batch_size (int, optional): Number of samples per batch. Defaults
                to 32.
            epochs (int, optional): Number of training epochs. Defaults to 10.
            img_size (int, optional): Size (height and width) to which input
                images are resized. Defaults to 224.
            freeze (int, optional): Number of layers to freeze during training.
                Defaults to 2.
            optimizer (str, optional): Optimizer to use for training (e.g.,
                "SGD", "Adam"). Defaults to "SGD".
            lr (float, optional): Learning rate for the optimizer. Defaults to
                1e-3.
            device (str, optional): Device to use for training ("cpu" or
                "cuda"). Defaults to "cpu".
        """
        self.model_path = model_path
        self.dataset_yaml = dataset_yaml
        self.output_dir = output_dir
        self.experiment_name = experiment_name
        self.batch_size = batch_size
        self.epochs = epochs
        self.img_size = img_size
        self.freeze = freeze
        self.optimizer = optimizer
        self.lr = lr
        self.device = device

    def train(self):
        """
        Trains a YOLO model using the specified configuration parameters.

        Loads the YOLO model from the provided model path and trains it using
            the dataset and hyperparameters defined in the instance attributes.
            Training results and the trained model are returned.

        Returns:
            tuple: A tuple containing the trained YOLO model and the training
                results object.
        """
        print("🔧 Loading model:", self.model_path)
        model = YOLO(self.model_path)

        results = model.train(
            data=self.dataset_yaml,
            epochs=self.epochs,
            batch=self.batch_size,
            imgsz=self.img_size,
            device=self.device,
            # workers=4,
            optimizer=self.optimizer,
            lr0=self.lr,
            momentum=0.9,
            # weight_decay=0.0005,
            freeze=self.freeze,
            project=self.output_dir,
            name=self.experiment_name,
            pretrained=True
        )

        return model, results

    def save_model(self, model: YOLO, path: str) -> None:
        """
        Saves the given YOLO model to the specified file path.

        Args:
            model (YOLO): The YOLO model instance to be saved.
            path (str): The file path where the model will be saved.

        Returns:
            None

        Side Effects:
            - Creates the directory specified in `path` if it does not
                exist.
            - Saves the model to the specified path.
            - Prints a confirmation message with the save location.
        """
        if os.path.isdir(path):
            path = os.path.join(path, "best.pt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        model.save(path)
        print(f"✅ Model saved to: {path}")
