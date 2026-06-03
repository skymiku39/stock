namespace CSharpT4
{
    partial class Form1
    {
        /// <summary>
        /// 設計工具所需的變數。
        /// </summary>
        private System.ComponentModel.IContainer components = null;

        /// <summary>
        /// 清除任何使用中的資源。
        /// </summary>
        /// <param name="disposing">如果應該處置 Managed 資源則為 true，否則為 false。</param>
        protected override void Dispose(bool disposing)
        {
            if (disposing && (components != null))
            {
                components.Dispose();
            }
            base.Dispose(disposing);
        }

        #region Windows Form 設計工具產生的程式碼

        /// <summary>
        /// 此為設計工具支援所需的方法 - 請勿使用程式碼編輯器修改這個方法的內容。
        ///
        /// </summary>
        private void InitializeComponent()
        {
            this.components = new System.ComponentModel.Container();
            System.ComponentModel.ComponentResourceManager resources = new System.ComponentModel.ComponentResourceManager(typeof(Form1));
            this.txLogonID = new System.Windows.Forms.TextBox();
            this.btnLogon = new System.Windows.Forms.Button();
            this.label1 = new System.Windows.Forms.Label();
            this.label2 = new System.Windows.Forms.Label();
            this.txLogonPass = new System.Windows.Forms.TextBox();
            this.cbStoAcct = new System.Windows.Forms.ComboBox();
            this.label3 = new System.Windows.Forms.Label();
            this.label4 = new System.Windows.Forms.Label();
            this.cbFuAcct = new System.Windows.Forms.ComboBox();
            this.label5 = new System.Windows.Forms.Label();
            this.btnAddAccCa = new System.Windows.Forms.Button();
            this.txCaPass = new System.Windows.Forms.TextBox();
            this.btnLogout = new System.Windows.Forms.Button();
            this.txStatus = new System.Windows.Forms.TextBox();
            this.groupBox1 = new System.Windows.Forms.GroupBox();
            this.label11 = new System.Windows.Forms.Label();
            this.label10 = new System.Windows.Forms.Label();
            this.label9 = new System.Windows.Forms.Label();
            this.label8 = new System.Windows.Forms.Label();
            this.cbOctType = new System.Windows.Forms.ComboBox();
            this.cbBuySell = new System.Windows.Forms.ComboBox();
            this.cbPriceType = new System.Windows.Forms.ComboBox();
            this.cbOrdType = new System.Windows.Forms.ComboBox();
            this.txAmount = new System.Windows.Forms.TextBox();
            this.label7 = new System.Windows.Forms.Label();
            this.label6 = new System.Windows.Forms.Label();
            this.txPrice = new System.Windows.Forms.TextBox();
            this.xNoname = new System.Windows.Forms.Label();
            this.txProdCode = new System.Windows.Forms.TextBox();
            this.btnPlaceOrder = new System.Windows.Forms.Button();
            this.timer1 = new System.Windows.Forms.Timer(this.components);
            this.groupBox2 = new System.Windows.Forms.GroupBox();
            this.groupBox3 = new System.Windows.Forms.GroupBox();
            this.chkCaPassDefault = new System.Windows.Forms.CheckBox();
            this.txCaPath = new System.Windows.Forms.TextBox();
            this.label12 = new System.Windows.Forms.Label();
            this.groupBox4 = new System.Windows.Forms.GroupBox();
            this.txParsedPrice = new System.Windows.Forms.TextBox();
            this.txParsedErr = new System.Windows.Forms.TextBox();
            this.txParsedStatus = new System.Windows.Forms.TextBox();
            this.txParsedTime = new System.Windows.Forms.TextBox();
            this.txParsedVolume = new System.Windows.Forms.TextBox();
            this.txParsedCode = new System.Windows.Forms.TextBox();
            this.txParsedOrdseq = new System.Windows.Forms.TextBox();
            this.txParsedOrdno = new System.Windows.Forms.TextBox();
            this.txParsedAccount = new System.Windows.Forms.TextBox();
            this.txParsedBranch = new System.Windows.Forms.TextBox();
            this.label15 = new System.Windows.Forms.Label();
            this.cbTradReply = new System.Windows.Forms.ComboBox();
            this.label14 = new System.Windows.Forms.Label();
            this.label13 = new System.Windows.Forms.Label();
            this.cbTradUnsolicited = new System.Windows.Forms.ComboBox();
            this.chkTradUnsolicited = new System.Windows.Forms.CheckBox();
            this.toolTip1 = new System.Windows.Forms.ToolTip(this.components);
            this.groupBox1.SuspendLayout();
            this.groupBox2.SuspendLayout();
            this.groupBox3.SuspendLayout();
            this.groupBox4.SuspendLayout();
            this.SuspendLayout();
            // 
            // txLogonID
            // 
            this.txLogonID.Location = new System.Drawing.Point(86, 32);
            this.txLogonID.Name = "txLogonID";
            this.txLogonID.Size = new System.Drawing.Size(114, 22);
            this.txLogonID.TabIndex = 1;
            // 
            // btnLogon
            // 
            this.btnLogon.Location = new System.Drawing.Point(424, 32);
            this.btnLogon.Name = "btnLogon";
            this.btnLogon.Size = new System.Drawing.Size(103, 23);
            this.btnLogon.TabIndex = 3;
            this.btnLogon.Text = "Login";
            this.btnLogon.UseVisualStyleBackColor = true;
            this.btnLogon.Click += new System.EventHandler(this.btnLogon_Click);
            // 
            // label1
            // 
            this.label1.AutoSize = true;
            this.label1.Location = new System.Drawing.Point(23, 35);
            this.label1.Name = "label1";
            this.label1.Size = new System.Drawing.Size(41, 12);
            this.label1.TabIndex = 4;
            this.label1.Text = "登入ID";
            // 
            // label2
            // 
            this.label2.AutoSize = true;
            this.label2.Location = new System.Drawing.Point(23, 69);
            this.label2.Name = "label2";
            this.label2.Size = new System.Drawing.Size(53, 12);
            this.label2.TabIndex = 5;
            this.label2.Text = "登入密碼";
            // 
            // txLogonPass
            // 
            this.txLogonPass.Location = new System.Drawing.Point(86, 66);
            this.txLogonPass.Name = "txLogonPass";
            this.txLogonPass.PasswordChar = '*';
            this.txLogonPass.Size = new System.Drawing.Size(114, 22);
            this.txLogonPass.TabIndex = 2;
            // 
            // cbStoAcct
            // 
            this.cbStoAcct.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbStoAcct.FormattingEnabled = true;
            this.cbStoAcct.Location = new System.Drawing.Point(86, 41);
            this.cbStoAcct.Name = "cbStoAcct";
            this.cbStoAcct.Size = new System.Drawing.Size(208, 20);
            this.cbStoAcct.TabIndex = 0;
            this.cbStoAcct.SelectedIndexChanged += new System.EventHandler(this.cbStoAcct_SelectedIndexChanged);
            // 
            // label3
            // 
            this.label3.AutoSize = true;
            this.label3.Location = new System.Drawing.Point(23, 44);
            this.label3.Name = "label3";
            this.label3.Size = new System.Drawing.Size(53, 12);
            this.label3.TabIndex = 8;
            this.label3.Text = "證券帳戶";
            // 
            // label4
            // 
            this.label4.AutoSize = true;
            this.label4.Location = new System.Drawing.Point(23, 73);
            this.label4.Name = "label4";
            this.label4.Size = new System.Drawing.Size(53, 12);
            this.label4.TabIndex = 10;
            this.label4.Text = "期貨帳戶";
            // 
            // cbFuAcct
            // 
            this.cbFuAcct.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbFuAcct.FormattingEnabled = true;
            this.cbFuAcct.Location = new System.Drawing.Point(86, 70);
            this.cbFuAcct.Name = "cbFuAcct";
            this.cbFuAcct.Size = new System.Drawing.Size(208, 20);
            this.cbFuAcct.TabIndex = 1;
            this.cbFuAcct.SelectedIndexChanged += new System.EventHandler(this.cbFuAcct_SelectedIndexChanged);
            // 
            // label5
            // 
            this.label5.AutoSize = true;
            this.label5.Location = new System.Drawing.Point(23, 144);
            this.label5.Name = "label5";
            this.label5.Size = new System.Drawing.Size(53, 12);
            this.label5.TabIndex = 11;
            this.label5.Text = "憑證密碼";
            // 
            // btnAddAccCa
            // 
            this.btnAddAccCa.Location = new System.Drawing.Point(424, 135);
            this.btnAddAccCa.Name = "btnAddAccCa";
            this.btnAddAccCa.Size = new System.Drawing.Size(103, 23);
            this.btnAddAccCa.TabIndex = 4;
            this.btnAddAccCa.Text = "加入憑證驗章";
            this.btnAddAccCa.UseVisualStyleBackColor = true;
            this.btnAddAccCa.Click += new System.EventHandler(this.btnAddAccCa_Click);
            // 
            // txCaPass
            // 
            this.txCaPass.Location = new System.Drawing.Point(86, 137);
            this.txCaPass.Name = "txCaPass";
            this.txCaPass.PasswordChar = '*';
            this.txCaPass.Size = new System.Drawing.Size(114, 22);
            this.txCaPass.TabIndex = 3;
            // 
            // btnLogout
            // 
            this.btnLogout.Location = new System.Drawing.Point(424, 65);
            this.btnLogout.Name = "btnLogout";
            this.btnLogout.Size = new System.Drawing.Size(103, 23);
            this.btnLogout.TabIndex = 4;
            this.btnLogout.Text = "Logout";
            this.btnLogout.UseVisualStyleBackColor = true;
            this.btnLogout.Click += new System.EventHandler(this.btnLogout_Click);
            // 
            // txStatus
            // 
            this.txStatus.Location = new System.Drawing.Point(18, 704);
            this.txStatus.Multiline = true;
            this.txStatus.Name = "txStatus";
            this.txStatus.Size = new System.Drawing.Size(597, 62);
            this.txStatus.TabIndex = 4;
            // 
            // groupBox1
            // 
            this.groupBox1.Controls.Add(this.label11);
            this.groupBox1.Controls.Add(this.label10);
            this.groupBox1.Controls.Add(this.label9);
            this.groupBox1.Controls.Add(this.label8);
            this.groupBox1.Controls.Add(this.cbOctType);
            this.groupBox1.Controls.Add(this.cbBuySell);
            this.groupBox1.Controls.Add(this.cbPriceType);
            this.groupBox1.Controls.Add(this.cbOrdType);
            this.groupBox1.Controls.Add(this.txAmount);
            this.groupBox1.Controls.Add(this.label7);
            this.groupBox1.Controls.Add(this.label6);
            this.groupBox1.Controls.Add(this.txPrice);
            this.groupBox1.Controls.Add(this.xNoname);
            this.groupBox1.Controls.Add(this.txProdCode);
            this.groupBox1.Controls.Add(this.btnPlaceOrder);
            this.groupBox1.Location = new System.Drawing.Point(18, 336);
            this.groupBox1.Name = "groupBox1";
            this.groupBox1.Size = new System.Drawing.Size(596, 171);
            this.groupBox1.TabIndex = 2;
            this.groupBox1.TabStop = false;
            this.groupBox1.Text = "3. 下單";
            // 
            // label11
            // 
            this.label11.AutoSize = true;
            this.label11.Location = new System.Drawing.Point(253, 93);
            this.label11.Name = "label11";
            this.label11.Size = new System.Drawing.Size(29, 12);
            this.label11.TabIndex = 15;
            this.label11.Text = "盤別";
            // 
            // label10
            // 
            this.label10.AutoSize = true;
            this.label10.Location = new System.Drawing.Point(253, 58);
            this.label10.Name = "label10";
            this.label10.Size = new System.Drawing.Size(29, 12);
            this.label10.TabIndex = 14;
            this.label10.Text = "倉別";
            // 
            // label9
            // 
            this.label9.AutoSize = true;
            this.label9.Location = new System.Drawing.Point(253, 24);
            this.label9.Name = "label9";
            this.label9.Size = new System.Drawing.Size(29, 12);
            this.label9.TabIndex = 13;
            this.label9.Text = "價別";
            // 
            // label8
            // 
            this.label8.AutoSize = true;
            this.label8.Location = new System.Drawing.Point(23, 24);
            this.label8.Name = "label8";
            this.label8.Size = new System.Drawing.Size(41, 12);
            this.label8.TabIndex = 12;
            this.label8.Text = "買賣別";
            // 
            // cbOctType
            // 
            this.cbOctType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbOctType.FormattingEnabled = true;
            this.cbOctType.Items.AddRange(new object[] {
            "自動",
            "新倉",
            "平倉",
            "當沖"});
            this.cbOctType.Location = new System.Drawing.Point(295, 55);
            this.cbOctType.Name = "cbOctType";
            this.cbOctType.Size = new System.Drawing.Size(81, 20);
            this.cbOctType.TabIndex = 5;
            // 
            // cbBuySell
            // 
            this.cbBuySell.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbBuySell.FormattingEnabled = true;
            this.cbBuySell.Items.AddRange(new object[] {
            "買",
            "賣"});
            this.cbBuySell.Location = new System.Drawing.Point(86, 21);
            this.cbBuySell.Name = "cbBuySell";
            this.cbBuySell.Size = new System.Drawing.Size(100, 20);
            this.cbBuySell.TabIndex = 0;
            // 
            // cbPriceType
            // 
            this.cbPriceType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbPriceType.FormattingEnabled = true;
            this.cbPriceType.Location = new System.Drawing.Point(295, 21);
            this.cbPriceType.Name = "cbPriceType";
            this.cbPriceType.Size = new System.Drawing.Size(81, 20);
            this.cbPriceType.TabIndex = 4;
            // 
            // cbOrdType
            // 
            this.cbOrdType.DropDownStyle = System.Windows.Forms.ComboBoxStyle.DropDownList;
            this.cbOrdType.FormattingEnabled = true;
            this.cbOrdType.Location = new System.Drawing.Point(295, 90);
            this.cbOrdType.Name = "cbOrdType";
            this.cbOrdType.Size = new System.Drawing.Size(81, 20);
            this.cbOrdType.TabIndex = 6;
            // 
            // txAmount
            // 
            this.txAmount.Location = new System.Drawing.Point(86, 125);
            this.txAmount.Name = "txAmount";
            this.txAmount.Size = new System.Drawing.Size(100, 22);
            this.txAmount.TabIndex = 3;
            this.txAmount.Text = "1";
            // 
            // label7
            // 
            this.label7.AutoSize = true;
            this.label7.Location = new System.Drawing.Point(23, 128);
            this.label7.Name = "label7";
            this.label7.Size = new System.Drawing.Size(53, 12);
            this.label7.TabIndex = 6;
            this.label7.Text = "委託數量";
            // 
            // label6
            // 
            this.label6.AutoSize = true;
            this.label6.Location = new System.Drawing.Point(23, 93);
            this.label6.Name = "label6";
            this.label6.Size = new System.Drawing.Size(53, 12);
            this.label6.TabIndex = 5;
            this.label6.Text = "委託價格";
            // 
            // txPrice
            // 
            this.txPrice.Location = new System.Drawing.Point(86, 90);
            this.txPrice.Name = "txPrice";
            this.txPrice.Size = new System.Drawing.Size(100, 22);
            this.txPrice.TabIndex = 2;
            this.txPrice.Text = "0.1";
            // 
            // xNoname
            // 
            this.xNoname.AutoSize = true;
            this.xNoname.Location = new System.Drawing.Point(23, 58);
            this.xNoname.Name = "xNoname";
            this.xNoname.Size = new System.Drawing.Size(53, 12);
            this.xNoname.TabIndex = 3;
            this.xNoname.Text = "商品代碼";
            // 
            // txProdCode
            // 
            this.txProdCode.Location = new System.Drawing.Point(86, 55);
            this.txProdCode.Name = "txProdCode";
            this.txProdCode.Size = new System.Drawing.Size(100, 22);
            this.txProdCode.TabIndex = 1;
            this.txProdCode.Text = "TXO18000D2";
            // 
            // btnPlaceOrder
            // 
            this.btnPlaceOrder.Location = new System.Drawing.Point(424, 123);
            this.btnPlaceOrder.Name = "btnPlaceOrder";
            this.btnPlaceOrder.Size = new System.Drawing.Size(103, 23);
            this.btnPlaceOrder.TabIndex = 7;
            this.btnPlaceOrder.Text = "送單";
            this.btnPlaceOrder.UseVisualStyleBackColor = true;
            this.btnPlaceOrder.Click += new System.EventHandler(this.btnPlaceOrder_Click);
            // 
            // groupBox2
            // 
            this.groupBox2.Controls.Add(this.btnLogon);
            this.groupBox2.Controls.Add(this.txLogonID);
            this.groupBox2.Controls.Add(this.label1);
            this.groupBox2.Controls.Add(this.btnLogout);
            this.groupBox2.Controls.Add(this.label2);
            this.groupBox2.Controls.Add(this.txLogonPass);
            this.groupBox2.Location = new System.Drawing.Point(18, 21);
            this.groupBox2.Name = "groupBox2";
            this.groupBox2.Size = new System.Drawing.Size(596, 103);
            this.groupBox2.TabIndex = 0;
            this.groupBox2.TabStop = false;
            this.groupBox2.Text = "1. 登入使用者(ID)";
            // 
            // groupBox3
            // 
            this.groupBox3.Controls.Add(this.chkCaPassDefault);
            this.groupBox3.Controls.Add(this.txCaPath);
            this.groupBox3.Controls.Add(this.label12);
            this.groupBox3.Controls.Add(this.cbStoAcct);
            this.groupBox3.Controls.Add(this.label3);
            this.groupBox3.Controls.Add(this.cbFuAcct);
            this.groupBox3.Controls.Add(this.label4);
            this.groupBox3.Controls.Add(this.txCaPass);
            this.groupBox3.Controls.Add(this.label5);
            this.groupBox3.Controls.Add(this.btnAddAccCa);
            this.groupBox3.Location = new System.Drawing.Point(18, 141);
            this.groupBox3.Name = "groupBox3";
            this.groupBox3.Size = new System.Drawing.Size(596, 180);
            this.groupBox3.TabIndex = 1;
            this.groupBox3.TabStop = false;
            this.groupBox3.Text = "2. 選擇交易帳戶並設定憑證驗章";
            // 
            // chkCaPassDefault
            // 
            this.chkCaPassDefault.AutoSize = true;
            this.chkCaPassDefault.Location = new System.Drawing.Point(217, 143);
            this.chkCaPassDefault.Name = "chkCaPassDefault";
            this.chkCaPassDefault.Size = new System.Drawing.Size(96, 16);
            this.chkCaPassDefault.TabIndex = 15;
            this.chkCaPassDefault.Text = "預設憑證密碼";
            this.chkCaPassDefault.UseVisualStyleBackColor = true;
            this.chkCaPassDefault.CheckedChanged += new System.EventHandler(this.chkCaPassDefault_CheckedChanged);
            // 
            // txCaPath
            // 
            this.txCaPath.Location = new System.Drawing.Point(86, 105);
            this.txCaPath.Name = "txCaPath";
            this.txCaPath.Size = new System.Drawing.Size(290, 22);
            this.txCaPath.TabIndex = 2;
            this.txCaPath.Text = "C:\\ekey\\";
            this.txCaPath.MouseDown += new System.Windows.Forms.MouseEventHandler(this.txCaPath_MouseDown);
            // 
            // label12
            // 
            this.label12.AutoSize = true;
            this.label12.Location = new System.Drawing.Point(23, 108);
            this.label12.Name = "label12";
            this.label12.Size = new System.Drawing.Size(53, 12);
            this.label12.TabIndex = 14;
            this.label12.Text = "憑證路徑";
            // 
            // groupBox4
            // 
            this.groupBox4.Controls.Add(this.txParsedPrice);
            this.groupBox4.Controls.Add(this.txParsedErr);
            this.groupBox4.Controls.Add(this.txParsedStatus);
            this.groupBox4.Controls.Add(this.txParsedTime);
            this.groupBox4.Controls.Add(this.txParsedVolume);
            this.groupBox4.Controls.Add(this.txParsedCode);
            this.groupBox4.Controls.Add(this.txParsedOrdseq);
            this.groupBox4.Controls.Add(this.txParsedOrdno);
            this.groupBox4.Controls.Add(this.txParsedAccount);
            this.groupBox4.Controls.Add(this.txParsedBranch);
            this.groupBox4.Controls.Add(this.label15);
            this.groupBox4.Controls.Add(this.cbTradReply);
            this.groupBox4.Controls.Add(this.label14);
            this.groupBox4.Controls.Add(this.label13);
            this.groupBox4.Controls.Add(this.cbTradUnsolicited);
            this.groupBox4.Controls.Add(this.chkTradUnsolicited);
            this.groupBox4.Location = new System.Drawing.Point(18, 524);
            this.groupBox4.Name = "groupBox4";
            this.groupBox4.Size = new System.Drawing.Size(596, 174);
            this.groupBox4.TabIndex = 3;
            this.groupBox4.TabStop = false;
            this.groupBox4.Text = "4. 下單回傳值";
            // 
            // txParsedPrice
            // 
            this.txParsedPrice.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedPrice.Location = new System.Drawing.Point(257, 118);
            this.txParsedPrice.Name = "txParsedPrice";
            this.txParsedPrice.ReadOnly = true;
            this.txParsedPrice.Size = new System.Drawing.Size(78, 22);
            this.txParsedPrice.TabIndex = 10;
            this.txParsedPrice.Text = "價";
            this.toolTip1.SetToolTip(this.txParsedPrice, "價格");
            // 
            // txParsedErr
            // 
            this.txParsedErr.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedErr.Location = new System.Drawing.Point(341, 118);
            this.txParsedErr.Name = "txParsedErr";
            this.txParsedErr.ReadOnly = true;
            this.txParsedErr.Size = new System.Drawing.Size(162, 22);
            this.txParsedErr.TabIndex = 11;
            this.txParsedErr.Text = "伺服器訊息";
            this.toolTip1.SetToolTip(this.txParsedErr, "伺服器訊息");
            // 
            // txParsedStatus
            // 
            this.txParsedStatus.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedStatus.Location = new System.Drawing.Point(509, 90);
            this.txParsedStatus.Name = "txParsedStatus";
            this.txParsedStatus.ReadOnly = true;
            this.txParsedStatus.Size = new System.Drawing.Size(78, 22);
            this.txParsedStatus.TabIndex = 7;
            this.txParsedStatus.Text = "狀態代碼";
            this.toolTip1.SetToolTip(this.txParsedStatus, "狀態代碼");
            // 
            // txParsedTime
            // 
            this.txParsedTime.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedTime.Location = new System.Drawing.Point(425, 90);
            this.txParsedTime.Name = "txParsedTime";
            this.txParsedTime.ReadOnly = true;
            this.txParsedTime.Size = new System.Drawing.Size(78, 22);
            this.txParsedTime.TabIndex = 6;
            this.txParsedTime.Text = "時間";
            this.toolTip1.SetToolTip(this.txParsedTime, "交易時間");
            // 
            // txParsedVolume
            // 
            this.txParsedVolume.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedVolume.Location = new System.Drawing.Point(173, 118);
            this.txParsedVolume.Name = "txParsedVolume";
            this.txParsedVolume.ReadOnly = true;
            this.txParsedVolume.Size = new System.Drawing.Size(78, 22);
            this.txParsedVolume.TabIndex = 9;
            this.txParsedVolume.Text = "量";
            this.toolTip1.SetToolTip(this.txParsedVolume, "量");
            // 
            // txParsedCode
            // 
            this.txParsedCode.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedCode.Location = new System.Drawing.Point(91, 118);
            this.txParsedCode.Name = "txParsedCode";
            this.txParsedCode.ReadOnly = true;
            this.txParsedCode.Size = new System.Drawing.Size(78, 22);
            this.txParsedCode.TabIndex = 8;
            this.txParsedCode.Text = "商品代碼";
            this.toolTip1.SetToolTip(this.txParsedCode, "商品代碼");
            // 
            // txParsedOrdseq
            // 
            this.txParsedOrdseq.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedOrdseq.Location = new System.Drawing.Point(341, 90);
            this.txParsedOrdseq.Name = "txParsedOrdseq";
            this.txParsedOrdseq.ReadOnly = true;
            this.txParsedOrdseq.Size = new System.Drawing.Size(78, 22);
            this.txParsedOrdseq.TabIndex = 5;
            this.txParsedOrdseq.Text = "委託書號";
            this.toolTip1.SetToolTip(this.txParsedOrdseq, "委託書號");
            // 
            // txParsedOrdno
            // 
            this.txParsedOrdno.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedOrdno.Location = new System.Drawing.Point(257, 90);
            this.txParsedOrdno.Name = "txParsedOrdno";
            this.txParsedOrdno.ReadOnly = true;
            this.txParsedOrdno.Size = new System.Drawing.Size(78, 22);
            this.txParsedOrdno.TabIndex = 4;
            this.txParsedOrdno.Text = "網路單號";
            this.toolTip1.SetToolTip(this.txParsedOrdno, "網路單號");
            // 
            // txParsedAccount
            // 
            this.txParsedAccount.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedAccount.Location = new System.Drawing.Point(173, 90);
            this.txParsedAccount.Name = "txParsedAccount";
            this.txParsedAccount.ReadOnly = true;
            this.txParsedAccount.Size = new System.Drawing.Size(78, 22);
            this.txParsedAccount.TabIndex = 3;
            this.txParsedAccount.Text = "帳戶";
            this.toolTip1.SetToolTip(this.txParsedAccount, "交易帳號");
            // 
            // txParsedBranch
            // 
            this.txParsedBranch.BackColor = System.Drawing.SystemColors.InactiveCaption;
            this.txParsedBranch.Location = new System.Drawing.Point(91, 90);
            this.txParsedBranch.Name = "txParsedBranch";
            this.txParsedBranch.ReadOnly = true;
            this.txParsedBranch.Size = new System.Drawing.Size(78, 22);
            this.txParsedBranch.TabIndex = 2;
            this.txParsedBranch.Text = "分公司";
            this.toolTip1.SetToolTip(this.txParsedBranch, "分公司");
            // 
            // label15
            // 
            this.label15.AutoSize = true;
            this.label15.Location = new System.Drawing.Point(23, 93);
            this.label15.Name = "label15";
            this.label15.Size = new System.Drawing.Size(53, 12);
            this.label15.TabIndex = 5;
            this.label15.Text = "電文解析";
            // 
            // cbTradReply
            // 
            this.cbTradReply.FormattingEnabled = true;
            this.cbTradReply.Location = new System.Drawing.Point(122, 61);
            this.cbTradReply.Name = "cbTradReply";
            this.cbTradReply.Size = new System.Drawing.Size(453, 20);
            this.cbTradReply.TabIndex = 1;
            this.cbTradReply.TextChanged += new System.EventHandler(this.cbTradReply_TextChanged);
            // 
            // label14
            // 
            this.label14.AutoSize = true;
            this.label14.Location = new System.Drawing.Point(23, 64);
            this.label14.Name = "label14";
            this.label14.Size = new System.Drawing.Size(77, 12);
            this.label14.TabIndex = 3;
            this.label14.Text = "下單回傳電文";
            // 
            // label13
            // 
            this.label13.AutoSize = true;
            this.label13.Location = new System.Drawing.Point(23, 38);
            this.label13.Name = "label13";
            this.label13.Size = new System.Drawing.Size(77, 12);
            this.label13.TabIndex = 2;
            this.label13.Text = "主動回報電文";
            // 
            // cbTradUnsolicited
            // 
            this.cbTradUnsolicited.FormattingEnabled = true;
            this.cbTradUnsolicited.Location = new System.Drawing.Point(122, 35);
            this.cbTradUnsolicited.Name = "cbTradUnsolicited";
            this.cbTradUnsolicited.Size = new System.Drawing.Size(453, 20);
            this.cbTradUnsolicited.TabIndex = 0;
            this.cbTradUnsolicited.TextChanged += new System.EventHandler(this.cbTradUnsolicited_TextChanged);
            // 
            // chkTradUnsolicited
            // 
            this.chkTradUnsolicited.AutoSize = true;
            this.chkTradUnsolicited.Location = new System.Drawing.Point(102, -1);
            this.chkTradUnsolicited.Name = "chkTradUnsolicited";
            this.chkTradUnsolicited.Size = new System.Drawing.Size(155, 16);
            this.chkTradUnsolicited.TabIndex = 0;
            this.chkTradUnsolicited.Text = "接收主動回報 (預設關閉)";
            this.chkTradUnsolicited.UseVisualStyleBackColor = true;
            this.chkTradUnsolicited.CheckedChanged += new System.EventHandler(this.chkTradUnsolicited_CheckedChanged);
            // 
            // toolTip1
            // 
            this.toolTip1.ShowAlways = true;
            // 
            // Form1
            // 
            this.AutoScaleDimensions = new System.Drawing.SizeF(6F, 12F);
            this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
            this.ClientSize = new System.Drawing.Size(638, 776);
            this.Controls.Add(this.groupBox4);
            this.Controls.Add(this.groupBox3);
            this.Controls.Add(this.groupBox2);
            this.Controls.Add(this.groupBox1);
            this.Controls.Add(this.txStatus);
            this.Icon = ((System.Drawing.Icon)(resources.GetObject("$this.Icon")));
            this.Name = "Form1";
            this.Text = "非正式範例 (稍微正式點)";
            this.FormClosing += new System.Windows.Forms.FormClosingEventHandler(this.Form1_FormClosing);
            this.Load += new System.EventHandler(this.Form1_Load);
            this.groupBox1.ResumeLayout(false);
            this.groupBox1.PerformLayout();
            this.groupBox2.ResumeLayout(false);
            this.groupBox2.PerformLayout();
            this.groupBox3.ResumeLayout(false);
            this.groupBox3.PerformLayout();
            this.groupBox4.ResumeLayout(false);
            this.groupBox4.PerformLayout();
            this.ResumeLayout(false);
            this.PerformLayout();

        }

        #endregion

        private System.Windows.Forms.TextBox txLogonID;
        private System.Windows.Forms.Button btnLogon;
        private System.Windows.Forms.Label label1;
        private System.Windows.Forms.Label label2;
        private System.Windows.Forms.TextBox txLogonPass;
        private System.Windows.Forms.ComboBox cbStoAcct;
        private System.Windows.Forms.Label label3;
        private System.Windows.Forms.Label label4;
        private System.Windows.Forms.ComboBox cbFuAcct;
        private System.Windows.Forms.Label label5;
        private System.Windows.Forms.Button btnAddAccCa;
        private System.Windows.Forms.TextBox txCaPass;
        private System.Windows.Forms.Button btnLogout;
        private System.Windows.Forms.TextBox txStatus;
        private System.Windows.Forms.GroupBox groupBox1;
        private System.Windows.Forms.ComboBox cbOctType;
        private System.Windows.Forms.ComboBox cbBuySell;
        private System.Windows.Forms.ComboBox cbPriceType;
        private System.Windows.Forms.ComboBox cbOrdType;
        private System.Windows.Forms.TextBox txAmount;
        private System.Windows.Forms.Label label7;
        private System.Windows.Forms.Label label6;
        private System.Windows.Forms.TextBox txPrice;
        private System.Windows.Forms.Label xNoname;
        private System.Windows.Forms.TextBox txProdCode;
        private System.Windows.Forms.Button btnPlaceOrder;
        private System.Windows.Forms.Timer timer1;
        private System.Windows.Forms.Label label11;
        private System.Windows.Forms.Label label10;
        private System.Windows.Forms.Label label9;
        private System.Windows.Forms.Label label8;
        private System.Windows.Forms.GroupBox groupBox2;
        private System.Windows.Forms.GroupBox groupBox3;
        private System.Windows.Forms.GroupBox groupBox4;
        private System.Windows.Forms.TextBox txCaPath;
        private System.Windows.Forms.Label label12;
        private System.Windows.Forms.Label label13;
        private System.Windows.Forms.ComboBox cbTradUnsolicited;
        private System.Windows.Forms.CheckBox chkTradUnsolicited;
        private System.Windows.Forms.Label label14;
        private System.Windows.Forms.ComboBox cbTradReply;
        private System.Windows.Forms.TextBox txParsedBranch;
        private System.Windows.Forms.Label label15;
        private System.Windows.Forms.TextBox txParsedAccount;
        private System.Windows.Forms.TextBox txParsedOrdno;
        private System.Windows.Forms.TextBox txParsedOrdseq;
        private System.Windows.Forms.TextBox txParsedCode;
        private System.Windows.Forms.TextBox txParsedVolume;
        private System.Windows.Forms.TextBox txParsedTime;
        private System.Windows.Forms.TextBox txParsedStatus;
        private System.Windows.Forms.TextBox txParsedErr;
        private System.Windows.Forms.TextBox txParsedPrice;
        private System.Windows.Forms.ToolTip toolTip1;
        private System.Windows.Forms.CheckBox chkCaPassDefault;
    }
}

