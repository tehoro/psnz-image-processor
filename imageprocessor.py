import sys
import os
import re
import pandas as pd
import requests
from PyQt5.QtWidgets import (QApplication, QMainWindow, QPushButton, QFileDialog, 
                            QCheckBox, QVBoxLayout, QHBoxLayout, QWidget, QLabel, 
                            QProgressBar, QMessageBox, QTextEdit)
from PyQt5.QtCore import QThread, pyqtSignal, Qt
from PIL import Image, ExifTags
from io import BytesIO
import csv
from pathlib import Path
import warnings

# Ignore PIL warnings from large image files
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

class ImageProcessorThread(QThread):
    """Thread for processing images to avoid freezing the UI"""
    progress_update = pyqtSignal(int, str)
    processing_complete = pyqtSignal(bool, str)
    
    def __init__(self, csv_path, output_dir, limit_size, remove_exif, add_sequence):
        super().__init__()
        self.csv_path = csv_path
        self.output_dir = output_dir
        self.limit_size = limit_size
        self.remove_exif = remove_exif
        self.add_sequence = add_sequence
        self.fullsize_limit = (3840, 2160)
        self.thumbnail_size = (810, 810)
        self.jpeg_quality = 100
        self.sequence_dict = {}

    def run(self):
        try:
            # Read and validate CSV file
            # Read and validate CSV file
            df = pd.read_csv(self.csv_path, escapechar='\\', quoting=csv.QUOTE_MINIMAL)
            total_images = len(df)
            
            # Check required columns
            if 'File Name' not in df.columns or 'Image: URL' not in df.columns:
                self.processing_complete.emit(False, "CSV file missing required columns: 'File Name' and 'Image: URL'")
                return
                
            # Create directory structure
            fullsize_folder_name = "4K-size" if self.limit_size else "submitted-size"
            if self.remove_exif:
                fullsize_folder_name += "-exifremoved"
                thumbnail_folder_name = "thumbnails-exifremoved"
            else:
                thumbnail_folder_name = "thumbnails"
                
            fullsize_dir = Path(self.output_dir) / fullsize_folder_name
            thumbnail_dir = Path(self.output_dir) / thumbnail_folder_name
            
            fullsize_dir.mkdir(exist_ok=True, parents=True)
            thumbnail_dir.mkdir(exist_ok=True, parents=True)
            
            # Path for EXIF data CSV
            exif_csv_path = Path(self.output_dir) / "image_metadata.csv"
            
            # Process images - collect all the exif data as we go
            exif_data_list = []
            
            for i, (_, row) in enumerate(df.iterrows()):
                # Emit progress update (with just basic info) - no, not needed
                # self.progress_update.emit(int(i * 100 / total_images), f"Processing image {i+1}/{total_images}: {row['File Name']}")
                
                # Process the image
                result = self.process_image(row, fullsize_dir, thumbnail_dir)
                if result:
                    exif_data_list.append(result['exif_info'])
                    
                    # Emit a detailed progress update with detailed file info
                    detailed_message = (f"Processed {i+1}/{total_images}: {result['exif_info']['FileName']} "
                                      f"({result['original_size']}, {result['status']})")
                    self.progress_update.emit(int(i * 100 / total_images), detailed_message)
            
            # Write EXIF data to CSV
            self.write_exif_csv(exif_csv_path, exif_data_list)
            
            # Emit completion signal
            self.processing_complete.emit(True, f"Successfully processed {len(exif_data_list)} of {total_images} images")
            
        except Exception as e:
            self.processing_complete.emit(False, f"Error: {str(e)}")
    
    def pad_id_with_sequence(self, filename):
        """Add sequence number for multiple entries from the same ID"""
        if not self.add_sequence:
            return filename
            
        match = re.match(r'^(\d+)(.*)$', filename)
        if match:
            id_num, rest = match.groups()

            if id_num not in self.sequence_dict:
                self.sequence_dict[id_num] = 1
            sequence_num = self.sequence_dict[id_num]
            self.sequence_dict[id_num] += 1

            name_parts = rest.rsplit('.', 1)
            if len(name_parts) == 2:
                clean_title = name_parts[0].lstrip('- ')
                return f"{id_num}-{sequence_num} {clean_title}.{name_parts[1]}"
            clean_rest = rest.lstrip('- ')
            return f"{id_num}-{sequence_num} {clean_rest}"
        return filename
    
    def get_exif_data(self, img):
        """Extract EXIF data from image"""
        exif_data = {
            'DateTimeCreated': None,
            'DateTimeOriginal': None,
            'Width': img.width,
            'Height': img.height
        }
        
        try:
            # Get EXIF data if available
            exif = {ExifTags.TAGS.get(tag, tag): value 
                    for tag, value in img._getexif().items()} if hasattr(img, '_getexif') and img._getexif() else {}
            
            # Date/Time when the image was created/modified
            if 'DateTime' in exif:
                exif_data['DateTimeCreated'] = exif['DateTime']
            
            # Original Date/Time when the photo was taken
            if 'DateTimeOriginal' in exif:
                exif_data['DateTimeOriginal'] = exif['DateTimeOriginal']
            elif 'DateTimeDigitized' in exif:
                exif_data['DateTimeOriginal'] = exif['DateTimeDigitized']
        except Exception:
            pass
        
        return exif_data
    
    def process_image(self, row, fullsize_dir, thumbnail_dir):
        """Process a single image"""
        try:
            # Replace invalid Windows filename characters with underscore
            original_filename = row['File Name']
            filename = re.sub(r'[\\/:*?"<>|]', '_', self.pad_id_with_sequence(original_filename))
            filepath = fullsize_dir / filename
            filepath_small = thumbnail_dir / filename
            
            # Fetch the image
            response = requests.get(row['Image: URL'], timeout=15)
            
            if response.status_code != 200:
                return None
            
            # Open image and process it
            with Image.open(BytesIO(response.content)) as img:
                # Get EXIF data before any modifications
                exif_data = self.get_exif_data(img)
                
                # Add filename and original filename to exif data
                exif_info = {
                    'FileName': filename,
                    'OriginalFileName': original_filename,
                    'Width': exif_data['Width'],
                    'Height': exif_data['Height'],
                    'DateTimeCreated': exif_data['DateTimeCreated'],
                    'DateTimeOriginal': exif_data['DateTimeOriginal']
                }

                # Convert to RGB if needed
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # Record original size and resize if needed
                original_size = f"{img.width}x{img.height}"
                resized = False
                
                # Create a copy for processing
                processed_img = img.copy()
                
                if self.limit_size and (processed_img.width > self.fullsize_limit[0] or 
                                     processed_img.height > self.fullsize_limit[1]):
                    processed_img.thumbnail(self.fullsize_limit)
                    resized = True

                # Remove EXIF if requested
                if self.remove_exif:
                    new_img = Image.new('RGB', processed_img.size)
                    new_img.paste(processed_img)
                    processed_img = new_img

                # Save full-size image
                processed_img.save(filepath, "JPEG", quality=self.jpeg_quality)

                # Create and save thumbnail
                thumbnail = processed_img.copy()
                thumbnail.thumbnail(self.thumbnail_size)
                thumbnail.save(filepath_small, "JPEG")

                # Return status information
                return {
                    'exif_info': exif_info,
                    'status': "resized" if resized else "original size",
                    'original_size': original_size
                }
                
        except Exception:
            return None
    
    def write_exif_csv(self, exif_csv_path, exif_data_list):
      """Write EXIF data to CSV file"""
      with open(exif_csv_path, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['FileName', 'OriginalFileName', 'Width', 'Height', 
                     'DateTimeCreated', 'DateTimeOriginal']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, 
                                escapechar='\\', 
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        for data in exif_data_list:
            writer.writerow(data)

class PSNZImageProcessor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PSNZ Image Entries Processor")
        self.setGeometry(100, 100, 800, 600)
        self.csv_path = None
        self.output_dir = None
        self.init_ui()
        
    def init_ui(self):
        # Main widget and layout
        main_widget = QWidget()
        main_layout = QVBoxLayout()
        
        # Title
        title_label = QLabel("PSNZ Image Entries Processor")
        title_label.setStyleSheet("font-size: 18px; font-weight: bold;")
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)
        
        # Description
        desc_label = QLabel("This app processes image entries from a CSV file, resizing them and organizing them for competition judging.")
        desc_label.setWordWrap(True)
        main_layout.addWidget(desc_label)
        
        # File selection section
        main_layout.addWidget(QLabel("Step 1: Select CSV file with 'File Name' and 'Image: URL' columns"))
        csv_layout = QHBoxLayout()
        self.csv_label = QLabel("No file selected")
        self.csv_button = QPushButton("Browse...")
        self.csv_button.clicked.connect(self.select_csv)
        csv_layout.addWidget(self.csv_label)
        csv_layout.addWidget(self.csv_button)
        main_layout.addLayout(csv_layout)
        
        # Output directory section
        main_layout.addWidget(QLabel("Step 2: Select output directory"))
        output_layout = QHBoxLayout()
        self.output_label = QLabel("No directory selected")
        self.output_button = QPushButton("Browse...")
        self.output_button.clicked.connect(self.select_output_dir)
        output_layout.addWidget(self.output_label)
        output_layout.addWidget(self.output_button)
        main_layout.addLayout(output_layout)
        
        # Options section
        main_layout.addWidget(QLabel("Step 3: Set processing options"))
        options_layout = QVBoxLayout()
        
        self.limit_size_cb = QCheckBox("Limit image size to 3840x2160 pixels (4K)")
        self.limit_size_cb.setChecked(True)
        options_layout.addWidget(self.limit_size_cb)
        
        self.remove_exif_cb = QCheckBox("Remove EXIF metadata from images")
        self.remove_exif_cb.setChecked(True)
        options_layout.addWidget(self.remove_exif_cb)
        
        self.add_sequence_cb = QCheckBox("Add sequence # after ID for multiple images")
        self.add_sequence_cb.setChecked(False)
        options_layout.addWidget(self.add_sequence_cb)
        
        main_layout.addLayout(options_layout)
        
        # Process button
        main_layout.addWidget(QLabel("Step 4: Process images"))
        self.process_button = QPushButton("Process Images")
        self.process_button.setEnabled(False)
        self.process_button.clicked.connect(self.process_images)
        self.process_button.setStyleSheet("font-weight: bold; padding: 10px;")
        main_layout.addWidget(self.process_button)
        
        # Progress section
        progress_layout = QVBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        progress_layout.addWidget(self.progress_bar)
        
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMinimumHeight(150)
        progress_layout.addWidget(self.status_text)
        
        main_layout.addLayout(progress_layout)
        
        # Set layout
        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)
    
    def select_csv(self):
        """Open file dialog to select CSV file"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select CSV File", "", "CSV Files (*.csv)")
        
        if file_path:
            self.csv_path = file_path
            self.csv_label.setText(os.path.basename(file_path))
            self.update_process_button()
            
            # Auto-suggest output directory 
            # (this is just the parent directory now, we'll create the CSV-named folder later)
            if not self.output_dir:
                    self.output_dir = os.path.dirname(file_path)
                    self.output_label.setText(self.output_dir)
            
            # Preview CSV
            try:
                df = pd.read_csv(file_path, escapechar='\\', quoting=csv.QUOTE_MINIMAL)
                self.status_text.append(f"CSV contains {len(df)} image entries")
                
                # Validate CSV format
                if 'File Name' not in df.columns or 'Image: URL' not in df.columns:
                    self.status_text.append("⚠️ Warning: CSV missing required columns!")
                    QMessageBox.warning(self, "Missing Columns", 
                                      "CSV file must have 'File Name' and 'Image: URL' columns.")
            except Exception as e:
                self.status_text.append(f"Error reading CSV: {str(e)}")
    
    def select_output_dir(self):
        """Open dialog to select output directory"""
        # If we have a CSV path, start from its directory
        start_dir = os.path.dirname(self.csv_path) if self.csv_path else ""
        
        dir_path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", start_dir)
        
        if dir_path:
            self.output_dir = dir_path
            self.output_label.setText(dir_path)
            self.update_process_button()
    
    def update_process_button(self):
        """Enable process button if both CSV and output directory are selected"""
        self.process_button.setEnabled(bool(self.csv_path and self.output_dir))
    
    def process_images(self):
        """Start image processing thread"""
        if not self.csv_path or not self.output_dir:
            return
            
        # Create a subfolder named after the CSV file
        csv_name = os.path.splitext(os.path.basename(self.csv_path))[0]
        output_subdir = os.path.join(self.output_dir, csv_name)
        
        # Create output directory if it doesn't exist
        os.makedirs(output_subdir, exist_ok=True)
        
        # Show the user where files will be saved
        self.status_text.append(f"Files will be saved to: {output_subdir}")
        
        # Setup processing thread
        self.processor_thread = ImageProcessorThread(
            self.csv_path,
            output_subdir,
            self.limit_size_cb.isChecked(),
            self.remove_exif_cb.isChecked(),
            self.add_sequence_cb.isChecked()
        )
        
        # Connect signals
        self.processor_thread.progress_update.connect(self.update_progress)
        self.processor_thread.processing_complete.connect(self.processing_finished)
        
        # Disable UI elements during processing
        self.process_button.setEnabled(False)
        self.csv_button.setEnabled(False)
        self.output_button.setEnabled(False)
        self.limit_size_cb.setEnabled(False)
        self.remove_exif_cb.setEnabled(False)
        self.add_sequence_cb.setEnabled(False)
        
        # Clear status text and start processing
        self.status_text.clear()
        self.status_text.append("Processing started...")
        self.processor_thread.start()
    
    def update_progress(self, value, message):
        """Update progress bar and status text"""
        self.progress_bar.setValue(value)
        self.status_text.append(message)
        # Auto-scroll to bottom
        self.status_text.verticalScrollBar().setValue(
            self.status_text.verticalScrollBar().maximum())
    
    def processing_finished(self, success, message):
        """Handle completion of image processing"""
        # Re-enable UI elements
        self.process_button.setEnabled(True)
        self.csv_button.setEnabled(True)
        self.output_button.setEnabled(True)
        self.limit_size_cb.setEnabled(True)
        self.remove_exif_cb.setEnabled(True)
        self.add_sequence_cb.setEnabled(True)
        
        # Update status
        self.status_text.append(message)
        
        if success:
            # Calculate final output directory path to show and open
            csv_name = os.path.splitext(os.path.basename(self.csv_path))[0]
            final_output_dir = os.path.join(self.output_dir, csv_name)
            
            QMessageBox.information(self, "Processing Complete", 
                                  f"Image processing completed successfully!\n\nOutput directory: {final_output_dir}")
            # Open the output directory
            import subprocess
            if sys.platform == 'darwin':  # macOS
                subprocess.call(['open', final_output_dir])
            elif sys.platform == 'win32':  # Windows
                os.startfile(final_output_dir)
        else:
            QMessageBox.critical(self, "Processing Error", message)
        

def main():
    app = QApplication(sys.argv)
    window = PSNZImageProcessor()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
