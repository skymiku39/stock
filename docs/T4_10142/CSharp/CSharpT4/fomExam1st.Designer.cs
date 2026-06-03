namespace CSharpT4
{
    partial class fomExam1st
    {
        /// <summary>
        /// Required designer variable.
        /// </summary>
        private System.ComponentModel.IContainer components = null;

        /// <summary>
        /// Clean up any resources being used.
        /// </summary>
        /// <param name="disposing">true if managed resources should be disposed; otherwise, false.</param>
        protected override void Dispose(bool disposing)
        {
            if (disposing && (components != null))
            {
                components.Dispose();
            }
            base.Dispose(disposing);
        }

        #region Windows Form Designer generated code

        /// <summary>
        /// Required method for Designer support - do not modify
        /// the contents of this method with the code editor.
        /// </summary>
        private void InitializeComponent()
        {
            this.label1 = new System.Windows.Forms.Label();
            this.label2 = new System.Windows.Forms.Label();
            this.label3 = new System.Windows.Forms.Label();
            this.txUserID = new System.Windows.Forms.TextBox();
            this.label4 = new System.Windows.Forms.Label();
            this.txBranch = new System.Windows.Forms.TextBox();
            this.label5 = new System.Windows.Forms.Label();
            this.txAccount = new System.Windows.Forms.TextBox();
            this.label6 = new System.Windows.Forms.Label();
            this.label7 = new System.Windows.Forms.Label();
            this.cbFakeStockBS = new System.Windows.Forms.ComboBox();
            this.txFakeStockCode = new System.Windows.Forms.TextBox();
            this.txFakeStockPrice = new System.Windows.Forms.TextBox();
            this.txFakeStockQty = new System.Windows.Forms.TextBox();
            this.cbFakeStockPriceType = new System.Windows.Forms.ComboBox();
            this.cbFakeStockOrderType = new System.Windows.Forms.ComboBox();
            this.cbFakeFuOrderType = new System.Windows.Forms.ComboBox();
            this.cbFakeFuPriceType = new System.Windows.Forms.ComboBox();
            this.txFakeFuQty = new System.Windows.Forms.TextBox();
            this.txFakeFuPrice = new System.Windows.Forms.TextBox();
            this.txFakeFuCode = new System.Windows.Forms.TextBox();
            this.cbFakeFuBS = new System.Windows.Forms.ComboBox();
            this.label8 = new System.Windows.Forms.Label();
            this.btnExam1st = new System.Windows.Forms.Button();
            this.SuspendLayout();
            // 
            // label1
            // 
            this.label1.AutoSize = true;
            this.label1.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label1.ForeColor = System.Drawing.Color.Navy;
            this.label1.Location = new System.Drawing.Point(36, 45);
            this.label1.Name = "label1";
            this.label1.Size = new System.Drawing.Size(264, 16);
            this.label1.TabIndex = 0;
            this.label1.Text = "使用 API 前帳戶必須進行首登驗證。";
            // 
            // label2
            // 
            this.label2.AutoSize = true;
            this.label2.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label2.ForeColor = System.Drawing.Color.Navy;
            this.label2.Location = new System.Drawing.Point(36, 76);
            this.label2.Name = "label2";
            this.label2.Size = new System.Drawing.Size(200, 16);
            this.label2.TabIndex = 1;
            this.label2.Text = "已驗證帳戶請勿重複驗證。";
            // 
            // label3
            // 
            this.label3.AutoSize = true;
            this.label3.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label3.Location = new System.Drawing.Point(36, 134);
            this.label3.Name = "label3";
            this.label3.Size = new System.Drawing.Size(88, 16);
            this.label3.TabIndex = 2;
            this.label3.Text = "帳戶所屬ID";
            // 
            // txUserID
            // 
            this.txUserID.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txUserID.Location = new System.Drawing.Point(133, 131);
            this.txUserID.MaxLength = 10;
            this.txUserID.Name = "txUserID";
            this.txUserID.Size = new System.Drawing.Size(145, 27);
            this.txUserID.TabIndex = 3;
            this.txUserID.Text = "A123456789";
            // 
            // label4
            // 
            this.label4.AutoSize = true;
            this.label4.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label4.Location = new System.Drawing.Point(36, 173);
            this.label4.Name = "label4";
            this.label4.Size = new System.Drawing.Size(56, 16);
            this.label4.TabIndex = 4;
            this.label4.Text = "分公司";
            // 
            // txBranch
            // 
            this.txBranch.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txBranch.Location = new System.Drawing.Point(133, 170);
            this.txBranch.MaxLength = 7;
            this.txBranch.Name = "txBranch";
            this.txBranch.Size = new System.Drawing.Size(145, 27);
            this.txBranch.TabIndex = 5;
            // 
            // label5
            // 
            this.label5.AutoSize = true;
            this.label5.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label5.Location = new System.Drawing.Point(36, 212);
            this.label5.Name = "label5";
            this.label5.Size = new System.Drawing.Size(40, 16);
            this.label5.TabIndex = 6;
            this.label5.Text = "帳戶";
            // 
            // txAccount
            // 
            this.txAccount.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txAccount.Location = new System.Drawing.Point(133, 209);
            this.txAccount.MaxLength = 7;
            this.txAccount.Name = "txAccount";
            this.txAccount.Size = new System.Drawing.Size(145, 27);
            this.txAccount.TabIndex = 7;
            // 
            // label6
            // 
            this.label6.AutoSize = true;
            this.label6.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label6.ForeColor = System.Drawing.Color.Red;
            this.label6.Location = new System.Drawing.Point(12, 282);
            this.label6.Name = "label6";
            this.label6.Size = new System.Drawing.Size(436, 16);
            this.label6.TabIndex = 8;
            this.label6.Text = "-------- 以下假資料「非交易用」 切勿作為 API 使用參考 --------";
            // 
            // label7
            // 
            this.label7.AutoSize = true;
            this.label7.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label7.ForeColor = System.Drawing.Color.Maroon;
            this.label7.Location = new System.Drawing.Point(15, 345);
            this.label7.Name = "label7";
            this.label7.Size = new System.Drawing.Size(40, 16);
            this.label7.TabIndex = 9;
            this.label7.Text = "股票";
            // 
            // cbFakeStockBS
            // 
            this.cbFakeStockBS.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeStockBS.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeStockBS.ForeColor = System.Drawing.Color.Maroon;
            this.cbFakeStockBS.FormattingEnabled = true;
            this.cbFakeStockBS.Items.AddRange(new object[] {
            "B",
            "S"});
            this.cbFakeStockBS.Location = new System.Drawing.Point(68, 342);
            this.cbFakeStockBS.Name = "cbFakeStockBS";
            this.cbFakeStockBS.Size = new System.Drawing.Size(50, 24);
            this.cbFakeStockBS.TabIndex = 10;
            // 
            // txFakeStockCode
            // 
            this.txFakeStockCode.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeStockCode.ForeColor = System.Drawing.Color.Maroon;
            this.txFakeStockCode.Location = new System.Drawing.Point(124, 342);
            this.txFakeStockCode.MaxLength = 6;
            this.txFakeStockCode.Name = "txFakeStockCode";
            this.txFakeStockCode.Size = new System.Drawing.Size(67, 27);
            this.txFakeStockCode.TabIndex = 11;
            this.txFakeStockCode.Text = "2890";
            // 
            // txFakeStockPrice
            // 
            this.txFakeStockPrice.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeStockPrice.ForeColor = System.Drawing.Color.Maroon;
            this.txFakeStockPrice.Location = new System.Drawing.Point(197, 342);
            this.txFakeStockPrice.MaxLength = 11;
            this.txFakeStockPrice.Name = "txFakeStockPrice";
            this.txFakeStockPrice.Size = new System.Drawing.Size(67, 27);
            this.txFakeStockPrice.TabIndex = 12;
            this.txFakeStockPrice.Text = "18.2";
            // 
            // txFakeStockQty
            // 
            this.txFakeStockQty.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeStockQty.ForeColor = System.Drawing.Color.Maroon;
            this.txFakeStockQty.Location = new System.Drawing.Point(270, 342);
            this.txFakeStockQty.MaxLength = 1;
            this.txFakeStockQty.Name = "txFakeStockQty";
            this.txFakeStockQty.Size = new System.Drawing.Size(50, 27);
            this.txFakeStockQty.TabIndex = 13;
            this.txFakeStockQty.Text = "1";
            // 
            // cbFakeStockPriceType
            // 
            this.cbFakeStockPriceType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeStockPriceType.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeStockPriceType.ForeColor = System.Drawing.Color.Maroon;
            this.cbFakeStockPriceType.FormattingEnabled = true;
            this.cbFakeStockPriceType.Items.AddRange(new object[] {
            "LMT",
            "MKT",
            "MKP"});
            this.cbFakeStockPriceType.Location = new System.Drawing.Point(326, 342);
            this.cbFakeStockPriceType.Name = "cbFakeStockPriceType";
            this.cbFakeStockPriceType.Size = new System.Drawing.Size(58, 24);
            this.cbFakeStockPriceType.TabIndex = 14;
            // 
            // cbFakeStockOrderType
            // 
            this.cbFakeStockOrderType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeStockOrderType.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeStockOrderType.ForeColor = System.Drawing.Color.Maroon;
            this.cbFakeStockOrderType.FormattingEnabled = true;
            this.cbFakeStockOrderType.Items.AddRange(new object[] {
            "IOC",
            "FOK",
            "ROD"});
            this.cbFakeStockOrderType.Location = new System.Drawing.Point(390, 342);
            this.cbFakeStockOrderType.Name = "cbFakeStockOrderType";
            this.cbFakeStockOrderType.Size = new System.Drawing.Size(58, 24);
            this.cbFakeStockOrderType.TabIndex = 15;
            // 
            // cbFakeFuOrderType
            // 
            this.cbFakeFuOrderType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeFuOrderType.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeFuOrderType.ForeColor = System.Drawing.Color.Purple;
            this.cbFakeFuOrderType.FormattingEnabled = true;
            this.cbFakeFuOrderType.Items.AddRange(new object[] {
            "IOC",
            "FOK",
            "ROD"});
            this.cbFakeFuOrderType.Location = new System.Drawing.Point(390, 388);
            this.cbFakeFuOrderType.Name = "cbFakeFuOrderType";
            this.cbFakeFuOrderType.Size = new System.Drawing.Size(58, 24);
            this.cbFakeFuOrderType.TabIndex = 22;
            // 
            // cbFakeFuPriceType
            // 
            this.cbFakeFuPriceType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeFuPriceType.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeFuPriceType.ForeColor = System.Drawing.Color.Purple;
            this.cbFakeFuPriceType.FormattingEnabled = true;
            this.cbFakeFuPriceType.Items.AddRange(new object[] {
            "LMT",
            "MKT",
            "MKP"});
            this.cbFakeFuPriceType.Location = new System.Drawing.Point(326, 388);
            this.cbFakeFuPriceType.Name = "cbFakeFuPriceType";
            this.cbFakeFuPriceType.Size = new System.Drawing.Size(58, 24);
            this.cbFakeFuPriceType.TabIndex = 21;
            // 
            // txFakeFuQty
            // 
            this.txFakeFuQty.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeFuQty.ForeColor = System.Drawing.Color.Purple;
            this.txFakeFuQty.Location = new System.Drawing.Point(270, 388);
            this.txFakeFuQty.MaxLength = 1;
            this.txFakeFuQty.Name = "txFakeFuQty";
            this.txFakeFuQty.Size = new System.Drawing.Size(50, 27);
            this.txFakeFuQty.TabIndex = 20;
            this.txFakeFuQty.Text = "1";
            // 
            // txFakeFuPrice
            // 
            this.txFakeFuPrice.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeFuPrice.ForeColor = System.Drawing.Color.Purple;
            this.txFakeFuPrice.Location = new System.Drawing.Point(197, 388);
            this.txFakeFuPrice.MaxLength = 11;
            this.txFakeFuPrice.Name = "txFakeFuPrice";
            this.txFakeFuPrice.Size = new System.Drawing.Size(67, 27);
            this.txFakeFuPrice.TabIndex = 19;
            this.txFakeFuPrice.Text = "18200.0";
            // 
            // txFakeFuCode
            // 
            this.txFakeFuCode.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.txFakeFuCode.ForeColor = System.Drawing.Color.Purple;
            this.txFakeFuCode.Location = new System.Drawing.Point(124, 388);
            this.txFakeFuCode.MaxLength = 6;
            this.txFakeFuCode.Name = "txFakeFuCode";
            this.txFakeFuCode.Size = new System.Drawing.Size(67, 27);
            this.txFakeFuCode.TabIndex = 18;
            this.txFakeFuCode.Text = "TXFE2";
            // 
            // cbFakeFuBS
            // 
            this.cbFakeFuBS.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFakeFuBS.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.cbFakeFuBS.ForeColor = System.Drawing.Color.Purple;
            this.cbFakeFuBS.FormattingEnabled = true;
            this.cbFakeFuBS.Items.AddRange(new object[] {
            "B",
            "S"});
            this.cbFakeFuBS.Location = new System.Drawing.Point(68, 388);
            this.cbFakeFuBS.Name = "cbFakeFuBS";
            this.cbFakeFuBS.Size = new System.Drawing.Size(50, 24);
            this.cbFakeFuBS.TabIndex = 17;
            // 
            // label8
            // 
            this.label8.AutoSize = true;
            this.label8.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.label8.ForeColor = System.Drawing.Color.Purple;
            this.label8.Location = new System.Drawing.Point(15, 391);
            this.label8.Name = "label8";
            this.label8.Size = new System.Drawing.Size(40, 16);
            this.label8.TabIndex = 16;
            this.label8.Text = "期貨";
            // 
            // btnExam1st
            // 
            this.btnExam1st.Font = new System.Drawing.Font("PMingLiU", 12F, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, ((byte)(136)));
            this.btnExam1st.Location = new System.Drawing.Point(312, 209);
            this.btnExam1st.Name = "btnExam1st";
            this.btnExam1st.Size = new System.Drawing.Size(110, 27);
            this.btnExam1st.TabIndex = 23;
            this.btnExam1st.Text = "首登驗證";
            this.btnExam1st.UseVisualStyleBackColor = true;
            this.btnExam1st.Click += new System.EventHandler(this.btnExam1st_Click);
            // 
            // fomExam1st
            // 
            this.AutoScaleDimensions = new System.Drawing.SizeF(6F, 12F);
            this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
            this.ClientSize = new System.Drawing.Size(464, 465);
            this.Controls.Add(this.btnExam1st);
            this.Controls.Add(this.cbFakeFuOrderType);
            this.Controls.Add(this.cbFakeFuPriceType);
            this.Controls.Add(this.txFakeFuQty);
            this.Controls.Add(this.txFakeFuPrice);
            this.Controls.Add(this.txFakeFuCode);
            this.Controls.Add(this.cbFakeFuBS);
            this.Controls.Add(this.label8);
            this.Controls.Add(this.cbFakeStockOrderType);
            this.Controls.Add(this.cbFakeStockPriceType);
            this.Controls.Add(this.txFakeStockQty);
            this.Controls.Add(this.txFakeStockPrice);
            this.Controls.Add(this.txFakeStockCode);
            this.Controls.Add(this.cbFakeStockBS);
            this.Controls.Add(this.label7);
            this.Controls.Add(this.label6);
            this.Controls.Add(this.txAccount);
            this.Controls.Add(this.label5);
            this.Controls.Add(this.txBranch);
            this.Controls.Add(this.label4);
            this.Controls.Add(this.txUserID);
            this.Controls.Add(this.label3);
            this.Controls.Add(this.label2);
            this.Controls.Add(this.label1);
            this.Name = "fomExam1st";
            this.Text = "首登驗證";
            this.Load += new System.EventHandler(this.fomExam1st_Load);
            this.ResumeLayout(false);
            this.PerformLayout();

        }

        #endregion

        private System.Windows.Forms.Label label1;
        private System.Windows.Forms.Label label2;
        private System.Windows.Forms.Label label3;
        private System.Windows.Forms.TextBox txUserID;
        private System.Windows.Forms.Label label4;
        private System.Windows.Forms.TextBox txBranch;
        private System.Windows.Forms.Label label5;
        private System.Windows.Forms.TextBox txAccount;
        private System.Windows.Forms.Label label6;
        private System.Windows.Forms.Label label7;
        private System.Windows.Forms.ComboBox cbFakeStockBS;
        private System.Windows.Forms.TextBox txFakeStockCode;
        private System.Windows.Forms.TextBox txFakeStockPrice;
        private System.Windows.Forms.TextBox txFakeStockQty;
        private System.Windows.Forms.ComboBox cbFakeStockPriceType;
        private System.Windows.Forms.ComboBox cbFakeStockOrderType;
        private System.Windows.Forms.ComboBox cbFakeFuOrderType;
        private System.Windows.Forms.ComboBox cbFakeFuPriceType;
        private System.Windows.Forms.TextBox txFakeFuQty;
        private System.Windows.Forms.TextBox txFakeFuPrice;
        private System.Windows.Forms.TextBox txFakeFuCode;
        private System.Windows.Forms.ComboBox cbFakeFuBS;
        private System.Windows.Forms.Label label8;
        private System.Windows.Forms.Button btnExam1st;
    }
}