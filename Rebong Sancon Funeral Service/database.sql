-- ====================================================================
-- RebongSancon Management System: Database Schema for XAMPP (MySQL)
-- Laguna University Capstone Project - Group 15 (Pascual, Pejas, Pineda)
-- Database Name: rebongsancon_db
-- ====================================================================

CREATE DATABASE IF NOT EXISTS `rebongsancon_db` 
CHARACTER SET utf8mb4 
COLLATE utf8mb4_unicode_ci;

USE `rebongsancon_db`;

-- --------------------------------------------------------------------
-- 0. Table: Users for Authentication & Role-Based Access Control
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `users` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `username` VARCHAR(50) NOT NULL UNIQUE,
    `password` VARCHAR(255) NOT NULL,
    `full_name` VARCHAR(150) NULL,
    `email` VARCHAR(150) NULL,
    `contact_no` VARCHAR(50) NULL,
    `role` ENUM('Admin', 'Staff') NOT NULL DEFAULT 'Staff',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

INSERT IGNORE INTO `users` (`username`, `password`, `full_name`, `email`, `contact_no`, `role`) VALUES
('admin', 'admin123', 'Administrator Staff', 'admin@rebongsancon.com', '09170000000', 'Admin'),
('staff', 'staff123', 'Frontline Staff', 'staff@rebongsancon.com', '09180000000', 'Staff');

-- --------------------------------------------------------------------
-- 0.1 Table: Staff Attendance & Daily Time Records (In & Out)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `staff_attendance` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `user_id` INT NULL,
    `username` VARCHAR(50) NOT NULL,
    `log_date` DATE NOT NULL,
    `time_in` TIME NULL,
    `time_out` TIME NULL,
    `duration_hours` DECIMAL(5,2) DEFAULT 0.00,
    `status` VARCHAR(50) DEFAULT 'On Duty',
    `notes` VARCHAR(255) NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- Sample attendance seed
INSERT IGNORE INTO `staff_attendance` (`username`, `log_date`, `time_in`, `time_out`, `duration_hours`, `status`) VALUES
('staff', CURDATE(), '08:00:00', NULL, 0.00, 'On Duty');

-- --------------------------------------------------------------------
-- 1. Table: Caskets & Funeral Packages (Showroom QR Registry)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `caskets` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `casket_code` VARCHAR(50) NOT NULL UNIQUE,
    `name` VARCHAR(150) NOT NULL,
    `category` VARCHAR(100) DEFAULT 'Standard Casket Package',
    `material` VARCHAR(150) DEFAULT 'Hardwood',
    `dimensions` VARCHAR(100) DEFAULT 'Standard Full Size',
    `image_url` VARCHAR(255) NULL,
    `base_price` DECIMAL(10,2) NOT NULL,
    `stock` INT NOT NULL DEFAULT 1,
    `inclusions` JSON NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------------------
-- 2. Table: Selectable Funeral Service Add-Ons
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `service_addons` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `name` VARCHAR(150) NOT NULL,
    `price` DECIMAL(10,2) NOT NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------------------
-- 3. Table: Orders & Billing Invoices (From ESP32 Smart QR POS)
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `orders` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `invoice_no` VARCHAR(50) NOT NULL UNIQUE,
    `order_type` VARCHAR(50) NOT NULL DEFAULT 'Package',
    `deceased_name` VARCHAR(150) NOT NULL,
    `contact_person` VARCHAR(150) NOT NULL,
    `contact_number` VARCHAR(50) NULL,
    `casket_code` VARCHAR(50) NOT NULL,
    `casket_name` VARCHAR(150) NOT NULL,
    `custom_details` JSON NULL,
    `base_price` DECIMAL(10,2) NOT NULL,
    `addons_total` DECIMAL(10,2) DEFAULT 0.00,
    `subtotal` DECIMAL(10,2) NOT NULL,
    `senior_discount_applied` TINYINT(1) DEFAULT 0,
    `discount_amount` DECIMAL(10,2) DEFAULT 0.00,
    `total_amount` DECIMAL(10,2) NOT NULL,
    `down_payment` DECIMAL(10,2) DEFAULT 0.00,
    `remaining_balance` DECIMAL(10,2) NOT NULL,
    `payment_method` VARCHAR(50) DEFAULT 'Cash',
    `payment_status` VARCHAR(50) DEFAULT 'Partial (Deposit)',
    `order_status` VARCHAR(50) DEFAULT 'Active',
    `is_archived` TINYINT(1) DEFAULT 0,
    `archived_at` TIMESTAMP NULL,
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------------------
-- 4. Table: Order Add-On Line Items
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `order_addons` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `invoice_no` VARCHAR(50) NOT NULL,
    `addon_name` VARCHAR(150) NOT NULL,
    `addon_price` DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (`invoice_no`) REFERENCES `orders`(`invoice_no`) ON DELETE CASCADE
) ENGINE=InnoDB;

-- --------------------------------------------------------------------
-- 5. Table: Service Reservations & Chapel Schedules
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `reservations` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `reservation_code` VARCHAR(50) NOT NULL UNIQUE,
    `invoice_no` VARCHAR(50) NULL,
    `deceased_name` VARCHAR(150) NOT NULL,
    `contact_person` VARCHAR(150) NOT NULL,
    `contact_number` VARCHAR(50) NULL,
    `chapel_room` VARCHAR(100) DEFAULT 'Chapel A (St. Peter Suite)',
    `hearse_driver` VARCHAR(100) DEFAULT 'Kuya Danny (Black Glass Hearse)',
    `start_date` DATE NOT NULL,
    `end_date` DATE NOT NULL,
    `interment_date` VARCHAR(100) DEFAULT 'Pending Schedule',
    `cemetery` VARCHAR(150) DEFAULT 'Calamba Public Cemetery',
    `status` VARCHAR(50) DEFAULT 'Active',
    `created_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- --------------------------------------------------------------------
-- 6. Table: ESP32 Hardware QR Scan Event Logs
-- --------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `esp32_scan_logs` (
    `id` INT AUTO_INCREMENT PRIMARY KEY,
    `qr_code` VARCHAR(50) NOT NULL,
    `ip_address` VARCHAR(50) DEFAULT '127.0.0.1',
    `scanned_at` TIMESTAMP DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- ====================================================================
-- SEED DATA (Showroom Caskets, Packages & Service Add-ons)
-- ====================================================================

INSERT INTO `caskets` (`casket_code`, `name`, `category`, `material`, `dimensions`, `base_price`, `stock`, `inclusions`) VALUES
('CSK-OAK-01', 'Presidential Solid Oak Funeral Package', 'Hardwood Casket Package', 'Solid Red Oak with Velvet Interior', '82" L x 28" W x 23" H', 85000.00, 4, 
 '["Professional Embalming & Cosmetic Restoration", "5 Days Air-conditioned Chapel Vigil Setup", "Glass-walled Funeral Hearse to Cemetery", "Memorial Guest Registry Book & 100 Mass Cards", "Burial Permit Assistance & Coordination"]'),

('CSK-MAHOG-02', 'Executive Mahogany Elegance Package', 'Hardwood Casket Package', 'Polished Mahogany Wood with Crepe Lining', '80" L x 27" W x 22" H', 65000.00, 2, 
 '["Complete Embalming & Dressing Service", "4 Days Viewing at Chapel or Private Residence", "Dedicated Modern Hearse Escort", "Crucifix & Memorial Vigil Lamp Setup"]'),

('CSK-MET-03', 'Heritage Stainless Metal Sealer Package', 'Gasketed Metal Casket', '18-Gauge Protective Gasketed Steel', '83" L x 28" W x 24" H', 110000.00, 1, 
 '["Hermetically Sealed Protection", "Complete Embalming & Full Dressing", "7 Days Air-conditioned Premier Chapel Vigil", "Cadillac Funeral Hearse Service", "2 Floral Stand Arrangements & Register Stand"]'),

('CSK-PINE-04', 'Serenity Natural Pine Simple Package', 'Standard Wood Casket', 'Natural Pine Wood with Satin Lining', '80" L x 26" W x 22" H', 42000.00, 5, 
 '["Sanitization & Standard Embalming", "3 Days Chapel or Home Vigil", "Standard Funeral Hearse Escort"]')
ON DUPLICATE KEY UPDATE `name`=VALUES(`name`);

INSERT INTO `service_addons` (`name`, `price`) VALUES
('Extra Vigil Day Rental (Chapel or Home)', 2500.00),
('Premium Fresh Floral Wreath Setup', 3500.00),
('Extra Family Escort Van / Hearse', 4000.00),
('Printed Memorial Mass Programs (100 pcs)', 1500.00),
('Special Embalming Facial Restoration', 5000.00),
('Ceramic Keepsake Urn Upgrade', 6500.00)
ON DUPLICATE KEY UPDATE `price`=VALUES(`price`);
